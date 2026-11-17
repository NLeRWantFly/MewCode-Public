"""Durable clarification, proposal and explicit version-bound approval."""
import asyncio
import copy
import hashlib
import json
import time
import uuid

from mewcode.experts.decision_context import decision_background, decision_sources


PRESENTATION_VERSION = 2


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def inputs(node, flow, snapshot):
    # Notes are context for the model, not part of the confirmed proposal
    # scope. Keep them out of the fingerprint so adding a note cannot widen or
    # invalidate declared grants.
    return [
        {key: value for key, value in source.items()
         if key not in {"notes", "answer_notes", "notes_role", "context_notes"}}
        for source in decision_sources(node, flow, snapshot)
        if source.get("kind") not in {"stage_input_notes", "gate_answer_notes"}
    ]


def retire_legacy_prompt(snapshot, node_id, scope):
    """Retire a context-free gate question without answering or approving it."""
    stage = (snapshot.get("node_states") or {}).get(node_id) or {}
    pending = stage.get("pending_interaction") or {}
    if (pending.get("kind") != "ask_user" or pending.get("tool") != "AskUserQuestion"
            or pending.get("scope_hash") != scope + ":clarify"
            or [question.get("name") for question in pending.get("questions", [])] != ["requirements"]):
        return False
    stage.setdefault("interaction_history", []).append({"pending": copy.deepcopy(pending),
        "status": "retired_not_approved", "answered": False,
        "reason": "gate_proposal_first", "at": time.time()})
    stage.pop("pending_interaction")
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    ledger = ledger_for_run(snapshot)
    replacement = ledger.record(node_id, "maintenance", {"reason": "gate_proposal_first",
        "request_id": pending["request_id"], "approved": False}, source="host")
    for fact in ledger.view(node_id).history:
        if fact.superseded_by:
            continue
        if (fact.kind == "question" and fact.payload.get("request_id") == pending["request_id"]
                or fact.kind == "blocker" and fact.payload.get("kind") in {"human_question", "user_input"}
                and fact.payload.get("ref") == pending["request_id"]):
            ledger.supersede(fact.fact_id, replacement.fact_id)
    project_node_view(snapshot, node_id)
    return True


def confirmation_valid(node, flow, snapshot):
    from mewcode.experts.human_gate import gate_scope_hash
    receipt = (snapshot.get("gate_confirmations") or {}).get(node.node_id) or {}
    draft = (snapshot.get("gate_dialogues") or {}).get(node.node_id) or {}
    if not receipt and (snapshot.get('gate_reuses') or {}).get(node.node_id):
        from mewcode.experts.restart_authorizations import gate_reuse_valid
        return gate_reuse_valid(node, flow, snapshot)
    return bool(receipt.get("decision") == "continue" and receipt.get("scope_hash") == gate_scope_hash(node, flow.meta)
                and draft.get("proposal") and receipt.get("proposal_hash") == digest(draft["proposal"])
                and receipt.get("inputs_hash") == draft.get("inputs_hash") == digest(inputs(node, flow, snapshot)))


async def await_dialogue(*, node, flow, session, store, run_id, emit, proposal_builder=None):
    from mewcode.agent import HumanOnCallRequest
    from mewcode.experts.human_gate import gate_scope_hash
    from mewcode.experts.waits import wait_for_interaction
    scope = gate_scope_hash(node, flow.meta)
    store.mutate(run_id, lambda value: retire_legacy_prompt(value, node.node_id, scope), durable=True)
    snapshot = store.load(run_id) or {}
    if confirmation_valid(node, flow, snapshot):
        return True
    if snapshot.get('task_restart'):
        from mewcode.experts.restart_authorizations import reuse_gate
        if reuse_gate(store, run_id, node=node, flow=flow, snapshot=snapshot, project=session.work_dir):
            await emit({'type': 'note', 'node_id': node.node_id,
                'text': '已核对并复用原运行中仍有效的同范围批准；未创建新的用户答复或权限。'})
            return True
    from mewcode.experts.authorization_ledger import valid_request
    declared_grants = [valid_request(r) for r in (flow.meta.get('authorization_grants') or {}).get(node.node_id, [])]
    sources = inputs(node, flow, snapshot)
    source_hash = digest(sources)
    draft = (snapshot.get("gate_dialogues") or {}).get(node.node_id) or {}
    if (draft.get("scope_hash") != scope or draft.get("inputs_hash") != source_hash
            or draft.get("presentation_version") != PRESENTATION_VERSION):
        draft = {"scope_hash": scope, "inputs_hash": source_hash,
                 "presentation_version": PRESENTATION_VERSION, "requirements": str(draft.get("requirements") or "")}
    brief = (flow.meta.get("node_plan_briefs") or {}).get(node.node_id) or {}
    description = str(brief.get("summary") or node.responsibility)
    background = decision_background(node, flow, snapshot)

    def save():
        store.mutate(run_id, lambda value: value.setdefault("gate_dialogues", {}).__setitem__(node.node_id, dict(draft)), durable=True)

    async def ask(kind, questions, version):
        pending = (((store.load(run_id) or {}).get("node_states") or {}).get(node.node_id) or {}).get("pending_interaction") or {}
        resume = pending.get("scope_hash") == version and pending.get("kind") == kind
        request = HumanOnCallRequest(pending["request_id"] if resume else uuid.uuid4().hex,
                      "human_oncall",
                      pending["questions"] if resume else questions, asyncio.get_running_loop().create_future())
        request.host_authorization_requests = declared_grants
        answer = await wait_for_interaction(request, kind=kind, session=session, emit=emit,
            store=store, run_id=run_id, stage_key=node.node_id, role=node.role, resumed=resume, scope_hash=version)
        return request, answer

    while True:
        if not draft.get("proposal"):
            answer_notes_context = json.dumps({
                "answer_notes": draft.get("answer_notes") or {},
                "answer_notes_role": "context_only",
            }, ensure_ascii=False)
            prompt = (background + f"\n范围：{description}\n用户补充或追问：{draft['requirements'] or '暂无；使用当前已选参数。'}\n"
                      f"用户选项备注（仅作上下文，context_only）：{answer_notes_context}\n"
                      f"上游交接：{json.dumps(sources, ensure_ascii=False)}\n"
                      "结合上游交接和当前参数，先完成上下文协调，再用简洁自然语言说明将做什么、使用哪些模型、产物及具体确认范围。"
                      "中间产物与待试点核验项直接传给对应后续节点，不为它们泛问用户怎么办。"
                      "用户追问先解释，不将追问、修改意见或已选参数视作本版批准；当前参数优先于上游旧的缺失说明。"
                      "不发明预算、外发或写入授权；缺少具体范围时列明缺项。只拟方案，不执行、不调用工具。")
            proposal = await proposal_builder(prompt) if proposal_builder else ""
            draft["proposal"] = background
            if draft['requirements'].strip():
                # A clarification is a new displayed proposal even when a
                # coordinator returns the same wording. Otherwise its cached
                # question answer can replay the clarification forever.
                draft['proposal'] += '\n\n本轮补充或追问\n' + draft['requirements'].strip()
            if isinstance(proposal, str) and proposal.strip():
                draft["proposal"] += "\n\n建议方案\n" + proposal.strip()
            if declared_grants:
                draft['proposal'] += '\n具体授权范围（只在批准本版方案后生效）：\n' + json.dumps(declared_grants, ensure_ascii=False)
            save()
        version = digest(draft["proposal"])
        await emit({"type": "note", "node_id": node.node_id, "text": draft["proposal"]})
        request, answer = await ask("human_oncall", [{"name": "choice", "title": "确认「" + node.label + "」的方案",
            "question": "请核对下面的处理计划和具体范围，再决定是否继续；也可提出修改或追问。",
            "context": draft["proposal"] + "\n只有明确批准才继续；修改意见不代表批准。验收缺口仍保留。",
            "options": [{"value": "continue", "label": "批准这版方案", "description": "仅授权上述范围。"},
                        {"value": "pause", "label": "暂不执行", "description": "保留方案和检查点。"}],
            "allowOther": True, "required": True, "requires_explicit_selection": True}], scope + ":" + version)
        notes = copy.deepcopy(getattr(request, "answer_notes", {}) or {})
        if notes:
            draft["answer_notes"] = notes
            draft["answer_notes_role"] = "context_only"
            save()
        if not isinstance(answer, dict) or not answer.get("choice") or answer["choice"] == "pause":
            return False
        if answer["choice"] == "continue":
            # Bind the answer to the scope and evidence the human actually saw.
            current = store.load(run_id) or {}
            if gate_scope_hash(node, flow.meta) != scope or digest(inputs(node, flow, current)) != source_hash:
                await emit({"type": "note", "node_id": node.node_id,
                    "text": "方案范围或上游证据已变化，本次答复未形成授权；请重新确认当前版本。"})
                return False
            from mewcode.experts.authorization_ledger import record_authorization
            # Gate prose is kept as an approved contract fact, not parsed into
            # arbitrary file/network capabilities. Concrete elevation remains typed.
            store.mutate(run_id, lambda value: record_authorization(value, kind='gate_answer',
                scope={'operation_classes': ['other'], 'node_ids': [node.node_id],
                    'operation_digest': version}, quote=draft['proposal'],
                source_ref='gate:' + request.request_id, host_verified=True), durable=True)
            for index, grant in enumerate(declared_grants):
                store.mutate(run_id, lambda value, grant=grant, index=index: record_authorization(value,
                    kind='gate_answer', scope={**grant['scope'], 'operation_classes': [grant['operation_class']]},
                    quote='已批准门禁所展示的具体范围', source_ref=f'gate:{request.request_id}:{index}',
                    host_verified=True), durable=True)
            store.mutate(run_id, lambda value: value.setdefault("gate_confirmations", {}).__setitem__(node.node_id, {
                "decision": "continue", "scope_hash": scope, "proposal_hash": version,
                "inputs_hash": source_hash,
                "request_id": request.request_id, "confirmed_at": time.time(),
                **({"answer_notes": notes, "answer_notes_role": "context_only"} if notes else {}),
            }), durable=True)
            return True
        draft["requirements"] += "\n修改意见：" + str(answer["choice"])
        draft.pop("proposal", None)
        save()
