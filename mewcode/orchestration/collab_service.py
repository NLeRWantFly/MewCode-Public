"""Trusted user controls and bounded read projections for a bound run."""
from __future__ import annotations

import copy

from mewcode.kernel import collaboration as ledger
from mewcode.kernel.events import digest


def bound_record(store, design, conversation_id):
    if not design or design.get("orchestration_mode") != "collaborative" or not design.get("run_id"):
        return None
    record = store.load(design["run_id"])
    if (not record or record.get("conversation_id") != conversation_id
            or ledger.state(record)["task_id"] != design["task_id"]):
        raise ValueError("协作运行与当前任务绑定不一致")
    return record


def projection(record):
    """Compatibility shape for existing mobile and task-design consumers."""
    from mewcode.orchestration.task_view import collaboration_projection
    return collaboration_projection(record)


def bound_projection(store, design, conversation_id):
    """Query path: fail closed if the writer has not published this binding."""
    view = store.read_design(design['run_id'])
    if view is None:
        raise ValueError('协作运行读模型尚未就绪，请等待会话恢复完成')
    if (view.get('run_id') != design['run_id'] or view.get('conversation_id') != conversation_id
            or view.get('task_id') != design['task_id'] or not isinstance(view.get('collaboration'), dict)):
        raise ValueError('协作运行与当前任务绑定不一致')
    return view['collaboration']


def revision_changes(value, updates, request_id):
    """Validate every requested field before building one atomic proposal."""
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
        raise ValueError("修订请求身份无效")
    if not isinstance(updates, list) or not 1 <= len(updates) <= 30:
        raise ValueError('请提供 1 到 30 项明确修改')
    changes = {}
    for update in updates:
        if not isinstance(update, dict) or set(update) - {'entry_id', 'body', 'field'}:
            raise ValueError('修订条目格式无效')
        entry_id, body, field = update.get('entry_id'), update.get('body'), update.get('field', 'body')
        if not isinstance(body, str) or not body.strip() or len(body) > 16000:
            raise ValueError("请填写不超过 16000 字的完整新要求")
        if not isinstance(entry_id, str) or entry_id in changes:
            raise ValueError('修订条目身份缺失或重复')
        entry = value['entries'].get(entry_id)
        if (not entry or field not in {'body', 'method', 'acceptance'}
                or field == 'body' and entry['kind'] not in {'goal', 'constraint', 'acceptance', 'decision', 'assumption'}
                or field in {'method', 'acceptance'} and entry['kind'] != 'node'
                or field == 'method' and not value['contracts'].get(entry_id, {}).get('method')):
            raise ValueError('此入口仅可修订目标、约束、验收、决定、假设或已指定的方法')
        if entry.get('origin') == 'host':
            raise ValueError('这是系统策略，不能在这里修改')
        replacement = copy.deepcopy(entry)
        if field == 'method':
            replacement.setdefault('data', {}).setdefault('contract', {})['method'] = {
                'id': 'method_' + digest([request_id, body.strip()])[:16], 'description': body.strip()}
            from mewcode.kernel.contracts import design_contract
            design_contract(replacement['data']['contract'])
        elif field == 'acceptance':
            from mewcode.kernel.contracts import checks as validate_checks
            checks = copy.deepcopy(value['contracts'].get(entry_id, {}).get('checks', []))
            if len(checks) != 1 or checks[0].get('kind') != 'review':
                raise ValueError('文字验收修订仅支持单个独立复核检查；不能替换命令、文件或结构检查')
            criteria = [line.strip() for line in body.splitlines() if line.strip()]
            if criteria == checks[0]['criteria']:
                raise ValueError('验收标准没有变化')
            checks[0].update(criteria=criteria, version=checks[0]['version'] + 1)
            replacement.setdefault('data', {})['acceptance'] = criteria
            declared = replacement['data'].setdefault('contract', {})
            declared['checks'] = validate_checks(checks)
            # The compiled contract includes host-preserved prohibitions that
            # may be absent from the original node's sparse declaration.
            if 'forbidden_changes' in declared:
                declared['forbidden_changes'] = copy.deepcopy(value['contracts'][entry_id]['forbidden_changes'])
        else:
            replacement['body'] = body.strip()
        changes[entry_id] = replacement
    return changes


def user_revision(store, record, *, entry_id, body, expected_version, request_id, field="body"):
    if not isinstance(body, str) or not body.strip() or len(body) > 16000:
        raise ValueError("请填写不超过 16000 字的完整新要求")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
        raise ValueError("修订请求身份无效")
    def change(saved):
        value = ledger.state(saved)
        identity = digest([record["run_id"], request_id])
        source_id = "user_revision:" + request_id
        previous = value.get("user_sources", {}).get(source_id)
        if previous:
            if (previous != {"role": "user", "text": body.strip(), "entry_id": entry_id, "field": field}
                    or value["proposals"][identity]["payload"]["base_version"] != expected_version):
                raise ValueError("同一修订请求不能用于另一内容")
            return
        changes = revision_changes(value, [dict(entry_id=entry_id, body=body, field=field)], request_id)
        ledger.proposal(value, proposal_id=identity, changes=changes, evidence=[source_id],
            actor="user", base_version=expected_version, epoch=value["coordinator_epoch"])
        value.setdefault("user_sources", {})[source_id] = {"role": "user", "text": body.strip(), "entry_id": entry_id, "field": field}
        saved.update(status="paused", status_reason="要求修订已保存，等待核对后继续。")
    return store.mutate(record["run_id"], change, durable=True)


def user_recovery(store, record, *, gate_id, expected_version, request_id, action, reason, work_dir, catalog, model=None, budget_amount=None):
    """No model-callable tool exposes this host-user operation."""
    from mewcode.kernel.collaboration_recovery import resolve_gate, manual_candidate, verifier_switch_attempt, execution_recheck_gates, unstarted_retry_gates, open_gate
    from mewcode.kernel.acceptance import Acceptance
    from mewcode.kernel.models import RoleModels, ALIASES
    from mewcode.model_registry import ModelRef
    if model is not None and action not in {"switch_model", "switch_verifier", "switch_coordinator"}:
        raise ValueError("只有明确换绑时才可以选择模型")
    role = "coordinator" if action == "switch_coordinator" else "verifier" if action == "switch_verifier" else "executor"
    chosen = (RoleModels(catalog).require(model, role).as_dict()
              if action in {"switch_model", "switch_verifier", "switch_coordinator"} else None)
    def apply(saved):
        if saved.get("status") == "running" or saved.get("active_node_ids"):
            raise ValueError("请先暂停运行，待执行节点结束后再决定恢复方式")
        value = ledger.state(saved)
        gate = value.get("recovery_gates", {}).get(gate_id)
        if not gate and action in {"recheck_execution_evidence", "retry", "switch_model", "revise"}:
            offers = [*execution_recheck_gates(saved), *unstarted_retry_gates(saved)]
            offered = next((row for row in offers if row["id"] == gate_id), None)
            if offered:
                gate = open_gate(saved, **{key: offered[key] for key in
                    ("scope", "label", "reason", "kind", "execution_evidence_fingerprint")}, keys=offered["node_ids"])
        if not gate:
            raise ValueError("没有找到对应的用户恢复关口")
        if action in {"switch_coordinator", "resume_coordinator"}:
            pool = RoleModels(catalog)
            ref = pool.require(chosen or value.get("coordinator"), "coordinator")
            roles = (saved.get("role_models") or {}).get("roles") or {}
            reviewer = pool.require(roles.get("reviewer"), "reviewer")
            if not pool.independent(ref, reviewer):
                raise ValueError("协调模型需要与独立复核模型来自不同厂商")
        elif action == "switch_verifier":
            if not verifier_switch_attempt(value, gate):
                raise ValueError("当前关口没有可换绑验收模型的已完成交付")
            from mewcode.kernel.collaboration_graph import effective_flow
            pool = RoleModels(catalog)
            roles = (saved.get("role_models") or {}).get("roles") or {}
            for key in gate["node_ids"]:
                node = effective_flow(saved).node_map[key]
                executor = pool.require((saved.get("locked_models") or {}).get(key) or node.model
                                        or roles.get(ALIASES.get(node.role, "executor")), "executor")
                if not pool.independent(executor, ModelRef.from_value(chosen)):
                    raise ValueError("新验收模型需要与当前执行模型来自不同厂商")
        elif chosen:
            pool = RoleModels(catalog)
            roles = (saved.get("role_models") or {}).get("roles") or {}
            from mewcode.kernel.collaboration_graph import effective_flow
            for key in gate["node_ids"]:
                node = effective_flow(saved).node_map[key].as_dict()
                ref = pool.require(chosen, ALIASES.get(node["role"], "executor"))
                reviewer = roles.get("reviewer" if node["role"] == "auditor" else "verifier")
                if reviewer and not pool.independent(ref, ModelRef.from_value(reviewer)):
                    raise ValueError("新执行模型需要与验收模型来自不同厂商")
        if action == "accept" and gate.get("status") == "waiting":
            report = manual_candidate(value, gate)
            if not report or not Acceptance(work_dir).unchanged(report.get("artifacts", [])):
                raise ValueError("待人工验收的产物已经变化，请重新执行并固定交付")
        if action == "recheck_execution_evidence" and gate.get("status") == "waiting":
            report = manual_candidate(value, gate)
            if not report or not Acceptance(work_dir).unchanged(report.get("artifacts", [])):
                raise ValueError("原交付产物已经变化，不能仅补齐宿主回执后重验")
        resolve_gate(saved, gate_id=gate_id, expected_version=expected_version, request_id=request_id,
            action=action, reason=reason, actor="user", model=chosen, budget_amount=budget_amount)
    return store.mutate(record["run_id"], apply, durable=True)
