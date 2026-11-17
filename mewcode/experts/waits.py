"""Durable, bounded human waits; heartbeat polling never cancels an answer."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import time

from mewcode.experts.recovery_state import save_interaction, finish_interaction


class BranchWaitPaused(Exception):
    def __init__(self, node_id, request_id='', *, reason='', error_kind='branch_wait'):
        self.node_id, self.request_id = node_id, request_id
        self.reason, self.error_kind = reason, error_kind
        super().__init__('branch_wait:' + node_id)


class HumanWaitTimeout(asyncio.CancelledError):
    def __init__(self):
        super().__init__("hitl_timeout")


class _WaitOutcome:
    def __init__(self, value, *, mode="user"):
        self.value, self.mode = value, mode


_QUESTION_EVIDENCE_KINDS = frozenset({"receipt", "artifact", "blocker", "handoff", "assistance", "progress"})
_HOST_CONTINUATION_TOOLS = frozenset({"IterationRecovery", "SchedulerRecovery"})


def _question_material(questions):
    material = []
    for question in questions if isinstance(questions, list) else []:
        if not isinstance(question, dict):
            material.append({"text": str(question), "options": []})
            continue
        options = []
        for option in question.get("options") or []:
            if isinstance(option, dict):
                options.append({key: option[key] for key in ("value", "id", "label", "description", "recommended")
                                if key in option})
            else:
                options.append(str(option))
        material.append({"name": question.get("name", ""), "type": question.get("type", ""),
            "text": question.get("question") or question.get("message") or "",
            "context": question.get("context") or "", "allow_other": bool(question.get('allow_other')),
            "requires_user_answer": bool(question.get('requires_user_answer')),
            "options": options})
    return material


def _question_evidence_ids(record, node_id):
    from mewcode.experts.node_ledger import ledger_for_run
    view = ledger_for_run(record).view(node_id)
    evidence = [fact for fact in view.history if fact.epoch == view.epoch and not fact.superseded_by
                and fact.kind in _QUESTION_EVIDENCE_KINDS]
    return sorted({fact.fact_id for fact in evidence})


def _prepare_question(record, *, bucket, stage_key, request, scope_hash, resumed):
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    from mewcode.experts.recovery_state import digest

    node_id = stage_key.split("::", 1)[0]
    stage = record.setdefault(bucket, {}).setdefault(stage_key, {})
    pending = stage.get("pending_interaction") or {}
    ledger = ledger_for_run(record)
    view = ledger.view(node_id)
    evidence_ids = _question_evidence_ids(record, node_id)
    material = [_question_material(request.questions), evidence_ids, scope_hash or ""]
    if getattr(request, "choice_bundle", None):
        material.append(request.choice_bundle.get("version"))
    fingerprint = digest(material)
    answered = {fact.payload.get("question_fact_id") for fact in view.history
                if fact.kind == "answer" and fact.source in {"user", "host"}}
    previous = next((fact for fact in reversed(view.history)
        if fact.kind == "question" and fact.epoch == view.epoch and not fact.superseded_by
        and fact.payload.get("question_fingerprint") == fingerprint and fact.fact_id not in answered), None)
    # Restoring the same durable request is a continuation of its existing
    # prompt, not a second question. A newly issued request with the same
    # question and unchanged evidence is suppressed below.
    resume_existing = bool(resumed and previous is not None
        and previous.payload.get("request_id") == request.request_id)
    fact = previous
    if fact is None:
        fact = ledger.record(node_id, "question", {
            "request_id": request.request_id, "tool": request.tool_name,
            "questions": copy.deepcopy(request.questions), "scope_hash": scope_hash or "",
            "question_fingerprint": fingerprint, "evidence_fact_ids": evidence_ids,
        }, source="host" if request.tool_name in _HOST_CONTINUATION_TOOLS else "user",
           identity=["interaction_question", request.request_id], epoch=view.epoch)
    for old in view.history:
        if (old.kind == 'question' and old.fact_id != fact.fact_id and not old.superseded_by
                and old.payload.get('request_id') == request.request_id):
            ledger.supersede(old.fact_id, fact.fact_id)
    if pending and pending.get("request_id") == request.request_id:
        pending.update(question_fact_id=fact.fact_id, question_fingerprint=fingerprint,
            evidence_fact_ids=evidence_ids)
    project_node_view(record, node_id)
    return {"node_id": node_id, "question_fact_id": fact.fact_id,
        "question_fingerprint": fingerprint, "evidence_fact_ids": evidence_ids,
        "suppressed": previous is not None and not resume_existing}


def _recommended_continuation(request, *, record, stage_key, bucket, request_id):
    from mewcode.kernel.gates import GateKind, classify, recommended
    if classify(record, stage_key, request=request) in {GateKind.AUTHORIZATION, GateKind.IRREVERSIBLE, GateKind.REQUIRED, GateKind.CONSTRAINT}:
        return None
    if record.get('orchestration_mode') == 'collaborative':
        row = record['collaboration']['questions'].get(getattr(request, 'collaboration_question_id', ''), {})
        from mewcode.orchestration.ask_gateway import valid_answer
        default = (row.get('optional_proof') or {}).get('default')
        return copy.deepcopy(default) if row.get('category') == 'optional' and valid_answer(request.questions, default) else None
    return recommended(request)


def _record_interaction_answer(record, *, bucket, stage_key, request_id, question_fact_id, mode,
                               notes=None):
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    stage = (record.get(bucket) or {}).get(stage_key) or {}
    pending = copy.deepcopy(stage.get("pending_interaction") or {})
    finish_interaction(record, bucket, stage_key, request_id)
    fact_id = question_fact_id or pending.get("question_fact_id")
    if fact_id:
        node_id = stage_key.split("::", 1)[0]
        ledger = ledger_for_run(record)
        question_ids = {f.fact_id for f in ledger.view(node_id).history if f.kind == 'question'
                        and f.payload.get('request_id') == request_id} | {fact_id}
        for answered_id in question_ids:
            payload = {
                "question_fact_id": answered_id, "request_id": request_id, "mode": mode,
                "host_continuation": mode == "auto_selected_recommended",
                "expands_authority": False if mode == "auto_selected_recommended" else None,
            }
            if isinstance(notes, dict) and notes:
                payload.update(answer_notes=copy.deepcopy(notes), notes_role="context_only")
            ledger.record(node_id, "answer", payload,
                source="host" if mode == "auto_selected_recommended" else "user",
                identity=["interaction_answer", answered_id, mode])
        from mewcode.experts.blocking_policy import refresh_clearances
        refresh_clearances(record, node_id)
        project_node_view(record, node_id)


def human_wait_timeout() -> float:
    try:
        value = float(os.environ.get("MEWCODE_EXPERT_HITL_SOFT_TIMEOUT", "120"))
        return value if math.isfinite(value) and value > 0 else 120.0
    except ValueError:
        return 120.0


async def wait_for_human(future, *, kind, emit, store, run_id, stage_key,
                         wait_id="", role="", heartbeat=30.0, soft_timeout=None,
                         hard_timeout=None, bucket="node_states", request=None,
                         question_fact_id=""):
    timeout = human_wait_timeout() if soft_timeout is None else float(soft_timeout)
    if hard_timeout is not None:
        timeout = min(timeout, float(hard_timeout))
    if not math.isfinite(timeout) or timeout <= 0 or heartbeat <= 0:
        raise ValueError("Invalid human-wait timeout")
    start, clock = time.time(), time.monotonic()
    from mewcode.kernel.gates import GateKind, classify, snapshot_before_timeout
    gate_kind = classify(store.load(run_id) or {}, stage_key, kind=kind, request=request)
    irreversible = gate_kind in {GateKind.AUTHORIZATION, GateKind.IRREVERSIBLE, GateKind.REQUIRED, GateKind.CONSTRAINT}
    snapshot_failed = False
    gate = getattr(request, 'host_gate', {})
    if gate.get('deadline') is not None:
        timeout = max(0, gate['deadline'] - start)
    base = {"type": "expert_role_wait", "role": role, "node_id": stage_key.split("::")[0],
            "wait_id": wait_id or stage_key, "kind": kind, "started_at": start}
    try:
        while not future.done():
            elapsed = time.monotonic() - clock
            if elapsed >= timeout and not irreversible and not snapshot_failed:
                if kind in {"ask_user", "human_oncall"}:
                    current = store.load(run_id) or {}
                    answer = _recommended_continuation(request, record=current, stage_key=stage_key,
                        bucket=bucket, request_id=wait_id)
                    if answer is not None:
                        try:
                            snapshot = await snapshot_before_timeout(store, run_id, stage_key, request=request)
                        except (OSError, ValueError, RuntimeError) as exc:
                            snapshot_failed = True
                            snapshot_error = str(exc)
                            store.mutate(run_id, lambda value: value.setdefault('kernel_gates', {}).setdefault(
                                wait_id, {}).update(snapshot_error=snapshot_error, status='snapshot_failed'), durable=True)
                            await emit({**base, 'active': True, 'snapshot_error': str(exc),
                                        'text': '快照未完成，继续等待你的选择。'})
                            continue
                        if future.done():
                            return future.result()
                        fresh = store.load(run_id) or {}
                        if classify(fresh, stage_key, kind=kind, request=request) in {
                                GateKind.AUTHORIZATION, GateKind.IRREVERSIBLE, GateKind.REQUIRED, GateKind.CONSTRAINT}:
                            irreversible = True
                            continue
                        from mewcode.kernel.gates import scope_identity
                        scope_current = getattr(request, 'scope_current', None)
                        if ((callable(scope_current) and not scope_current()) or (not callable(scope_current) and gate
                                and scope_identity(fresh, stage_key, request, getattr(request, 'gate_scope_hash', '')) != gate['scope_identity'])):
                            snapshot_failed = True
                            continue
                        store.mutate(run_id, lambda value: value.setdefault('kernel_gates', {}).setdefault(
                            wait_id, {}).update(status='timeout_selected', selection_source='timeout',
                                snapshot_id=snapshot['snapshot_id'], selected=answer), durable=True)
                        await emit({**base, 'type': 'gate_timeout_selected', 'selection_source': 'timeout',
                                    'snapshot_id': snapshot['snapshot_id'], 'selected': answer})
                        return _WaitOutcome(answer, mode="auto_selected_recommended")
                    at = time.time()
                    store.mutate(run_id, lambda value: value.setdefault(bucket, {}).setdefault(stage_key, {}).update(
                        status="paused", wait_timeout={"kind": kind, "request_id": wait_id, "at": at,
                            "question_fact_id": question_fact_id, "reason": "business_question_timeout"}))
                    raise BranchWaitPaused(stage_key.split("::", 1)[0], wait_id)
                store.mutate(run_id, lambda value: value.setdefault(bucket, {}).setdefault(stage_key, {}).update(
                    status="paused", wait_timeout={"kind": kind, "request_id": wait_id, "at": time.time()}))
                store.transition(run_id, "paused", reason="等待人工响应超时；问题已保留，继续时重新提问。")
                raise HumanWaitTimeout()
            await emit({**base, "active": True, "elapsed": elapsed, 'gate_kind': gate_kind.value,
                        'deadline': None if irreversible or snapshot_failed else start + timeout,
                        'timeout_consequence': '持续等待你的明确选择。' if irreversible else '先保存快照，再按推荐项继续。'})
            await asyncio.wait({future}, timeout=float(heartbeat) if irreversible or snapshot_failed else min(float(heartbeat), max(.001, timeout - elapsed)))
        return future.result()
    finally:
        if not future.done():
            future.cancel()
        await emit({**base, "active": False, "elapsed": time.monotonic() - clock, "completed_at": time.time()})


async def wait_for_interaction(request, *, kind, session, emit, store, run_id, stage_key,
                               role="", bucket="node_states", resumed=False, scope_hash=""):
    collaborative = ((store.load(run_id) or {}).get('orchestration_mode') == 'collaborative'
                     and not getattr(request, 'host_file_access', None))
    if collaborative and len(request.questions) > 3:
        answers = {}
        for offset in range(0, len(request.questions), 3):
            part = copy.copy(request)
            part.questions = copy.deepcopy(request.questions[offset:offset + 3])
            for index, question in enumerate(part.questions):
                question.setdefault('name', 'q' + str(offset + index))
            part.request_id = request.request_id + '_part_' + str(offset // 3)
            part.future = asyncio.get_running_loop().create_future()
            part.choice_bundle = None
            part.source_questions = None
            answers.update(await wait_for_interaction(part, kind=kind, session=session, emit=emit, store=store,
                run_id=run_id, stage_key=stage_key, role=role, bucket=bucket, resumed=resumed, scope_hash=scope_hash))
        return answers
    await _prepare_interaction_choices(request, session=session, emit=emit, store=store,
                                       run_id=run_id, stage_key=stage_key, bucket=bucket)
    if collaborative:
        from mewcode.orchestration.ask_gateway import prepare as prepare_collaboration, present
        from mewcode.kernel.gates import classify, GateKind
        row = {}
        def register_question(record):
            gate_kind = classify(record, stage_key, kind=kind, request=request)
            category = ('authorization' if gate_kind == GateKind.AUTHORIZATION else
                        'irreversible' if gate_kind == GateKind.IRREVERSIBLE else
                        'constraint' if gate_kind == GateKind.CONSTRAINT else 'business')
            proof = record['collaboration'].get('optional_defaults', {}).get(getattr(request, 'collaboration_optional_id', ''))
            row.update(prepare_collaboration(record, stage_key.split('::')[0], request.questions,
                category='optional' if proof and category == 'business' else category, optional_proof=proof))
        store.mutate(run_id, register_question, durable=True)
        request.collaboration_question_id = row['id']
        if row['status'] == 'answered':
            return copy.deepcopy(row['answer'])
        if row['status'] == 'blocked':
            raise BranchWaitPaused(stage_key.split('::')[0], request.request_id, reason=row['reason'])
        record = store.load(run_id)
        nodes = (record.get('locked_flow') or {}).get('nodes', [])
        request.collaboration_display_questions = present(request.questions,
            internal_ids=[node['node_id'] for node in nodes], labels={node['node_id']: node.get('label') or '当前步骤' for node in nodes})
    from mewcode.kernel.gates import prepare
    gate = {}
    store.mutate(run_id, lambda value: gate.update(prepare(value, stage_key, request,
        kind=kind, scope_hash=scope_hash, timeout=human_wait_timeout())), durable=True)
    request.host_gate, request.gate_scope_hash = gate, scope_hash
    previous_answer = (store.load(run_id) or {}).get('kernel_gate_answers', {}).get(gate['scope_identity'])
    if previous_answer:
        request.answer_notes = copy.deepcopy(previous_answer.get('notes') or {})
        return copy.deepcopy(previous_answer['answer'])
    # Parallel stages share one visible prompt slot. Persist before queuing.
    pending = {"kind": kind, "request_id": request.request_id, "tool": request.tool_name,
               "questions": request.questions, 'gate': gate}
    if getattr(request, "host_file_access", None):
        pending["host_file_access"] = copy.deepcopy(request.host_file_access)
    if getattr(request, "choice_bundle", None):
        pending.update(choice_bundle=request.choice_bundle,
                       source_questions=request.source_questions)
        pending['choice_instruction'] = getattr(request, 'choice_instruction', '')
    if scope_hash:
        pending["scope_hash"] = scope_hash
    store.mutate(run_id, lambda value: save_interaction(value, bucket, stage_key, pending), durable=True)
    lock = getattr(session, "_expert_interaction_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        session._expert_interaction_lock = lock
    queue = getattr(session, '_kernel_question_queue', None)
    if queue is None:
        queue = session._kernel_question_queue = {}
    queue[request.request_id] = (stage_key, request, kind)
    await asyncio.sleep(.025)
    try:
        async with lock:
            if kind == 'ask_user' and not request.future.done():
                entries = [(key, item) for key, item, surface in queue.values()
                           if surface == kind and not item.future.done() and all(
                               2 <= len(question.get('options') or []) <= 4 for question in item.questions)]
                if collaborative:
                    bounded, count = [], 0
                    for entry in entries:
                        if count + len(entry[1].questions) > 3:
                            break
                        bounded.append(entry)
                        count += len(entry[1].questions)
                    entries = bounded
                if len(entries) > 1 and entries[0][1] is request and (
                        all(item.choice_bundle for _, item in entries) or not any(item.choice_bundle for _, item in entries)):
                    from mewcode.kernel.gates import group_display
                    request.group_display = group_display(entries)
            return await _show_interaction(request, kind=kind, session=session, emit=emit, store=store,
                run_id=run_id, stage_key=stage_key, role=role, bucket=bucket, resumed=resumed, scope_hash=scope_hash)
    finally:
        queue.pop(request.request_id, None)
        if not request.future.done():
            request.future.cancel()


async def _prepare_interaction_choices(request, *, session, emit, store, run_id, stage_key,
                                       bucket="node_states", refresh=False):
    """Prepare before acquiring the prompt slot; never execute business work."""
    if getattr(request, "host_file_access", None) or getattr(request, "tool_name", '') == 'ReviewDecision':
        # ReviewDecision values already bind to host-validated outcomes.
        # Candidate generation must not add actions or change those values.
        return
    prepare = getattr(session, "interaction_choice_preparer", None)
    if not callable(prepare):
        return
    snapshot = store.load(run_id) or {}
    request.choice_resolution = "wait"
    request.choice_run_id = run_id
    stage = (snapshot.get(bucket) or {}).get(stage_key) or {}
    previous = stage.get("pending_interaction") or {}
    def signature(value):
        from mewcode.experts.authorization_ledger import entries
        from mewcode.experts.node_ledger import ledger_for_run
        node_id = stage_key.split("::")[0]
        current = (value.get(bucket) or {}).get(stage_key) or {}
        pending = current.get('pending_interaction') or {}
        return json.dumps({'flow': value.get('locked_flow'), 'profile': value.get('locked_profile'),
            'inputs': value.get('run_inputs'), 'epoch': ledger_for_run(value).view(node_id).epoch,
            'revision': current.get('input_revision'), 'authorizations': entries(value),
            'pending': {key: pending.get(key) for key in ('request_id', 'scope_hash', 'source_questions')},
            'upstream': {key: state.get('handoff') for key, state in (value.get('node_states') or {}).items()
                         if key != node_id and isinstance(state, dict)},
            'instruction': current.get('choice_instructions')}, sort_keys=True, ensure_ascii=False)
    frozen_signature = signature(copy.deepcopy(snapshot))
    request.choice_state_signature = lambda: signature(store.load(run_id) or {})
    if not getattr(request, "source_questions", None):
        restored = previous.get("source_questions") if previous.get("request_id") == request.request_id else None
        request.source_questions = copy.deepcopy(restored or request.questions)
    if not getattr(request, 'choice_instruction', ''):
        request.choice_instruction = previous.get('choice_instruction', '')
    await emit({"type": "interaction_choices_preparing", "run_id": run_id,
                "node_id": stage_key.split("::")[0], "request_id": request.request_id,
                "text": "正在查找候选／准备处理方案", "active": True})
    try:
        bundle = await prepare(request=request, snapshot=snapshot, node_id=stage_key.split("::")[0],
                               cached=previous.get("choice_bundle") or stage.get("prepared_choices"),
                               refresh=refresh)
        if not isinstance(bundle, dict):
            raise ValueError("选项准备器没有返回候选包")
        from mewcode.orchestration.interaction_choices import project_questions
        projected = project_questions(request.source_questions, bundle)
        def save(value):
            if signature(value) != frozen_signature:
                raise ValueError("节点代际、输入、授权或提问已经变化，未使用旧选项")
            current = value.setdefault(bucket, {}).setdefault(stage_key, {})
            if current.get("input_revision") != stage.get("input_revision"):
                raise ValueError("节点输入已变化，未使用旧选项")
            current["prepared_choices"] = copy.deepcopy(bundle)
            current.pop('choice_instructions', None)
        store.mutate(run_id, save, durable=True)
        request.choice_bundle = bundle
        request.questions = projected
    finally:
        await emit({"type": "interaction_choices_preparing", "run_id": run_id,
                    "node_id": stage_key.split("::")[0], "request_id": request.request_id,
                    "active": False})


async def _show_interaction(request, *, kind, session, emit, store, run_id, stage_key,
                            role, bucket, resumed, scope_hash):
    attribute = "_human_oncall_request" if kind == "human_oncall" else "_ask_user_request"
    existing = getattr(session, attribute, None)
    if existing is not None and existing is not request and not existing.future.done():
        raise RuntimeError("Another human interaction is already pending")
    pending = {"kind": kind, "request_id": request.request_id, "tool": request.tool_name,
               "questions": request.questions}
    if getattr(request, "choice_bundle", None):
        pending.update(choice_bundle=request.choice_bundle,
                       source_questions=request.source_questions)
        pending['choice_instruction'] = getattr(request, 'choice_instruction', '')
    if scope_hash:
        pending["scope_hash"] = scope_hash
    store.mutate(run_id, lambda value: save_interaction(value, bucket, stage_key, pending), durable=True)
    setattr(session, attribute, request)
    try:
        question = {}
        store.mutate(run_id, lambda value: question.update(_prepare_question(value,
            bucket=bucket, stage_key=stage_key, request=request, scope_hash=scope_hash,
            resumed=resumed)), durable=True)
        if question.get("suppressed"):
            at = time.time()
            def suppress(value):
                stage = value.setdefault(bucket, {}).setdefault(stage_key, {})
                stage.update(status="paused", wait_timeout={"kind": kind, "request_id": request.request_id,
                    "at": at, "question_fact_id": question.get("question_fact_id"),
                    "reason": "duplicate_question_suppressed"})
            store.mutate(run_id, suppress, durable=True)
            await emit({"type": "question_suppressed", "question_suppressed": True,
                "role": role, "node_id": question.get("node_id"), "request_id": request.request_id,
                "question_fingerprint": question.get("question_fingerprint"),
                "evidence_fact_ids": question.get("evidence_fact_ids") or []})
            raise BranchWaitPaused(question.get("node_id") or stage_key.split("::", 1)[0], request.request_id)
        if callable(getattr(request, "choice_state_signature", None)):
            request.choice_display_signature = request.choice_state_signature()
        display = getattr(request, 'group_display', request)
        setattr(session, attribute, display)
        if not getattr(request, 'group_answered', False):
            await emit({"type": kind, "role": role, "node_id": stage_key.split("::")[0],
                    "request_id": request.request_id, "tool": request.tool_name,
                    "questions": getattr(display, 'collaboration_display_questions', display.questions), "resumed": resumed, 'gate': getattr(display, 'host_gate', {}),
                    **({"choice_bundle": display.choice_bundle} if getattr(display, "choice_bundle", None) else {})})
        outcome = await wait_for_human(request.future, kind=kind, emit=emit, store=store, run_id=run_id,
            stage_key=stage_key, wait_id=request.request_id, role=role, bucket=bucket, request=display,
            question_fact_id=question.get("question_fact_id", ""))
        if display is not request:
            from mewcode.kernel.gates import distribute_group
            outcome = distribute_group(display, outcome)
        mode = outcome.mode if isinstance(outcome, _WaitOutcome) else "user"
        answer = outcome.value if isinstance(outcome, _WaitOutcome) else outcome
        choice_answers = copy.deepcopy(answer)
        if isinstance(answer, dict) and answer and getattr(request, "choice_bundle", None):
            from mewcode.orchestration.interaction_choices import resolve_answers
            resolved = resolve_answers(request.choice_bundle, answer,
                choice_inputs=getattr(request, "choice_inputs", {}))
            if resolved["actions"]:
                actions = resolved["actions"]
                def keep_action(value):
                    current = value.setdefault(bucket, {}).setdefault(stage_key, {})
                    current["choice_instructions"] = copy.deepcopy(actions)
                    current["choice_action_values"] = copy.deepcopy(resolved["values"])
                    current["choice_action_notes"] = copy.deepcopy(getattr(request, "answer_notes", {}))
                    current.setdefault("interaction_history", []).append({
                        "pending": copy.deepcopy(current.get("pending_interaction") or {}),
                        "status": "choice_action", "answered": True, "actions": copy.deepcopy(actions)})
                    if not any(item["action"] in {"defer", "manual"} for item in actions):
                        current.pop("pending_interaction", None)
                    else:
                        current["status"] = "paused"
                store.mutate(run_id, keep_action, durable=True)
                if any(item["action"] in {"defer", "manual"} for item in actions):
                    raise BranchWaitPaused(stage_key.split("::")[0], request.request_id,
                                           reason="已暂缓当前选择，问题与检查点保留。")
                # Re-prepare only after an explicit user action. This does not
                # retry business execution or add recovery rounds.
                request.future = asyncio.get_running_loop().create_future()
                request.choice_inputs = {}
                request.answer_notes = {}
                await _prepare_interaction_choices(request, session=session, emit=emit, store=store,
                    run_id=run_id, stage_key=stage_key, bucket=bucket, refresh=True)
                return await _show_interaction(request, kind=kind, session=session, emit=emit,
                    store=store, run_id=run_id, stage_key=stage_key, role=role,
                    bucket=bucket, resumed=True, scope_hash=scope_hash)
            answer = resolved["values"]
            request.resolved_answers = {name: ", ".join(value) if isinstance(value, list) else value
                                        for name, value in answer.items()}
        empty_answer = answer is None or (isinstance(answer, str) and not answer.strip())
        if isinstance(answer, dict):
            empty_answer = not answer or (request.tool_name != "StageRunInputs" and any(
                not str(answer.get(q.get('name') or ('choice' if kind == 'human_oncall' else 'q' + str(index))) or '').strip()
                for index, q in enumerate(request.questions)))
        if empty_answer:
            store.mutate(run_id, lambda value: value.setdefault(bucket, {}).setdefault(stage_key, {}).update(
                status='paused'), durable=True)
            raise BranchWaitPaused(stage_key.split('::')[0], request.request_id,
                                   reason='尚未提交有效回答，原问题与检查点已保留。')
        if getattr(request, 'collaboration_question_id', ''):
            from mewcode.orchestration.ask_gateway import answer as answer_collaboration
            store.mutate(run_id, lambda value: answer_collaboration(value, request.collaboration_question_id, answer,
                source=mode, choice_bundle=getattr(request, 'choice_bundle', None),
                choice_answers=choice_answers, choice_inputs=getattr(request, 'choice_inputs', {})), durable=True)
        store.mutate(run_id, lambda value: _record_interaction_answer(value, bucket=bucket,
            stage_key=stage_key, request_id=request.request_id, question_fact_id=question.get("question_fact_id", ""),
            mode=mode, notes=getattr(request, "answer_notes", {})), durable=True)
        gate = getattr(request, 'host_gate', {})
        if gate:
            store.mutate(run_id, lambda value: value.setdefault('kernel_gate_answers', {}).__setitem__(
                gate['scope_identity'], {'answer': copy.deepcopy(answer), 'mode': mode,
                    'notes': copy.deepcopy(getattr(request, 'answer_notes', {}))}), durable=True)
        return answer
    finally:
        if getattr(session, attribute, None) in (request, getattr(request, 'group_display', None)):
            setattr(session, attribute, None)
