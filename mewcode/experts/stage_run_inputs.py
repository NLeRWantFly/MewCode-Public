"""Collect declared inputs at their stage, independently of tool permissions."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid

from mewcode.orchestration.run_inputs import _is_empty, _normalize_value, stage_input_questions, validate_input_contract


def stage_input_notes(snapshot, node_id):
    """Return user notes for one node, separate from the run-input values."""
    stage = ((snapshot or {}).get("node_states") or {}).get(node_id) or {}
    entries = stage.get("input_notes") or {}
    if not isinstance(entries, dict):
        return {}
    return {
        str(request_id): copy.deepcopy(entry)
        for request_id, entry in entries.items()
        if isinstance(entry, dict) and isinstance(entry.get("notes"), dict)
    }


def _save_stage_input_notes(record, node_id, request_id, notes):
    if not isinstance(notes, dict) or not notes:
        return
    stage = record.setdefault("node_states", {}).setdefault(node_id, {})
    stage.setdefault("input_notes", {})[request_id] = {
        "notes": copy.deepcopy(notes),
        "notes_role": "context_only",
    }


async def collect_stage_run_inputs(*, node, flow, session, store, run_id, emit):
    from mewcode.agent import AskUserRequest
    from mewcode.experts.waits import wait_for_interaction
    declarations = flow.meta.get("run_input_contract", [])
    defined = validate_input_contract(declarations, node_ids=set(flow.node_map))
    if not defined:
        return {}
    # Parallel members and stages share one durable input ledger. Recheck
    # after the lock so a sibling never asks for an already supplied value.
    lock = getattr(session, "_stage_run_input_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        session._stage_run_input_lock = lock
    async with lock:
        branch_inputs = {edge.input_when["input"] for edge in flow.edges
                         if edge.from_node == node.node_id and edge.input_when is not None}
        for _ in range(len(defined) + 1):
            snapshot = store.load(run_id) or {}
            questions, normalized = stage_input_questions(declarations, snapshot.get("run_inputs", {}),
                                                          node.node_id, branch_inputs=branch_inputs)
            if not questions:
                effective = {name: value for name, value in normalized.items() if value}
                if any((snapshot.get("run_inputs") or {}).get(name) != value for name, value in effective.items()):
                    store.mutate(run_id, lambda value: value.setdefault("run_inputs", {}).update(effective), durable=True)
                return effective
            prepare = getattr(session, 'stage_parameter_preparer', None)
            internal = {'batch_size', 'pages_per_batch', 'batch_pages', 'max_retries', 'retry_delay',
                        'scratch_dir', 'temporary_directory', 'temp_dir'}
            internal_here = {item['name'] for item in questions if item['name'] in internal
                             and item['name'] not in branch_inputs and not defined[item['name']].get('required_when')}
            if callable(prepare) and internal_here:
                bundle = await prepare(questions=questions, snapshot=snapshot, node_id=node.node_id)
                automatic = {}
                for name, value in (bundle.get('system_answers') or {}).items():
                    if name not in internal_here:
                        continue
                    try:
                        automatic[name] = _normalize_value(value, defined[name])
                    except ValueError:
                        continue
                if automatic:
                    from mewcode.experts.node_ledger import ledger_for_run
                    from mewcode.experts.waits import BranchWaitPaused
                    stage_before = (snapshot.get('node_states') or {}).get(node.node_id) or {}
                    epoch_before = ledger_for_run(snapshot).view(node.node_id).epoch
                    def save_internal(record):
                        current_stage = (record.get('node_states') or {}).get(node.node_id) or {}
                        if (getattr(session, 'cancel_requested', False)
                                or record.get('status') in {'completed', 'terminated'}
                                or current_stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}
                                or (current_stage.get('recovery_case') or {}).get('closed_by_user')
                                or current_stage.get('input_revision', '') != stage_before.get('input_revision', '')
                                or ledger_for_run(record).view(node.node_id).epoch != epoch_before
                                or record.get('flow_fingerprint') != snapshot.get('flow_fingerprint')
                                or record.get('run_inputs', {}) != snapshot.get('run_inputs', {})):
                            raise BranchWaitPaused(node.node_id, reason='参数或检查点在准备期间已变化，旧内部选择未应用。',
                                                   error_kind='system_parameter_stale')
                        record.setdefault('run_inputs', {}).update(automatic)
                        record.setdefault('system_input_choices', {}).setdefault(node.node_id, []).append({
                            'values': copy.deepcopy(automatic), 'bundle_id': bundle['id'],
                            'version': bundle['version'], 'source': 'primary_model_not_authorization'})
                    store.mutate(run_id, save_internal, durable=True)
                    await emit({'type': 'expert_step', 'node_id': node.node_id, 'status': 'completed',
                                'label': '已由主模型整理内部处理参数'})
                    continue
                from mewcode.experts.waits import BranchWaitPaused
                raise BranchWaitPaused(node.node_id, reason='内部处理参数尚未准备好；由系统继续检查，不需要用户填写技术参数。',
                                       error_kind='system_parameter_blocked')
            from mewcode.experts.decision_context import decision_background
            background = decision_background(node, flow, snapshot)
            payload = [{"name": item["name"], "title": "继续「" + node.label + "」前需要确认",
                        "context": background if index == 0 else "",
                        "message": " · ".join(filter(None,
                        (item.get("label"), item.get("description")))) or item["name"],
                        "type": "radio" if item.get("options") else "text", "required": True,
                        "input_type": item.get("type", "text"),
                        "separator": item.get("separator", "\n"),
                        "allow_other": not bool(item.get("options")),
                        **({"options": [{**option, "label": option["label"] + ("（推荐）" if option.get("recommended") else "")}
                                        for option in item["options"]]} if item.get("options") else {})} for index, item in enumerate(questions)]
            scope = hashlib.sha256(json.dumps({"node_id": node.node_id, "contract": declarations,
                "questions": payload, "known": normalized}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            previous = ((snapshot.get("node_states") or {}).get(node.node_id) or {}).get("pending_interaction") or {}
            resumed = previous.get("scope_hash") == scope and previous.get("tool") == "StageRunInputs"
            request = AskUserRequest(previous["request_id"] if resumed else uuid.uuid4().hex,
                                     "StageRunInputs", payload, asyncio.get_running_loop().create_future())
            await emit({"type": "note", "node_id": node.node_id,
                        "text": f"到「{node.label}」了，补充本阶段参数后继续；参数答复不替代人工签认或工具权限。"})
            answers = await wait_for_interaction(request, kind="ask_user", session=session, emit=emit,
                store=store, run_id=run_id, stage_key=node.node_id, role=node.role, resumed=resumed, scope_hash=scope)
            if not isinstance(answers, dict) or set(answers) - {item["name"] for item in questions}:
                return None
            accepted = {}
            for item in questions:
                answer = answers.get(item["name"])
                if answer is None:
                    continue
                try:
                    actual = _normalize_value(answer, defined[item["name"]])
                except ValueError:
                    return None
                if _is_empty(actual):
                    continue
                if item.get("options") and actual not in {option["value"] for option in item["options"]}:
                    return None
                accepted[item["name"]] = actual
            if not accepted:
                return None
            store.mutate(run_id, lambda value: value.setdefault("run_inputs", {}).update(accepted), durable=True)
            notes = copy.deepcopy(getattr(request, "answer_notes", {}) or {})
            if notes:
                store.mutate(run_id, lambda value: _save_stage_input_notes(
                    value, node.node_id, request.request_id, notes), durable=True)
        raise ValueError("本阶段参数的条件仍无法确定，未执行节点")
