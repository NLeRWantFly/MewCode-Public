"""Compile the selected design into immutable, source-linked work contracts."""
from __future__ import annotations

import copy

from mewcode.kernel.collaboration import FORMAT
from mewcode.kernel.events import digest

ROLLING_FORMAT = "mewcode.collaboration.v2"
REQUIREMENT_KINDS = frozenset({"goal", "artifact", "input", "acceptance", "decision", "assumption"})
NON_REQUIREMENT_KINDS = frozenset({"node", "edge", "milestone", "constraint"})


def attach_design(spec, design, draft_state, *, sources=()):
    selected = design.get("orchestration_mode", "classic")
    spec.meta["orchestration_mode"] = selected
    if selected != "collaborative":
        spec.meta.pop("collaboration_spec", None)
        return
    materials = draft_state.get("materials") or {}
    candidate = draft_state.get("candidate") or {}
    document = candidate.get("document") or materials.get("document") or draft_state.get("document") or {}
    basis = candidate.get("history_basis") or draft_state.get("history_basis") or {}
    spec.meta["collaboration_spec"] = {
        "format": FORMAT, "task_id": design["task_id"], "goal": design.get("goal", ""),
        "material_sequence": materials.get("sequence", 0), "document": copy.deepcopy(document),
        "field_sources": copy.deepcopy(basis.get("entry_field_sources", {})),
        "source_boundary": copy.deepcopy(design.get("source_boundary")),
        "source_ids": copy.deepcopy(draft_state.get("source_ids", [])),
        "sources": copy.deepcopy(list(sources)),
    }
    if any(row.get("kind") == "milestone" and row.get("state") != "retired"
           and (row.get("data") or {}).get("deferred") is True for row in document.get("entries", [])):
        spec.meta["collaboration_spec"]["planning_mode"] = "rolling"


def _declared_ids(data, field):
    value = data.get(field)
    if value is None:
        return []
    from mewcode.kernel.contracts import text_list
    try:
        return text_list(value, ids=True)
    except ValueError as exc:
        raise ValueError(f"{field} 必须引用稳定条目 ID 列表") from exc


def _check_reference_types(identities, entries, *, constraint=False, label=""):
    for identity in identities:
        row = entries.get(identity)
        if row is None:
            raise ValueError(f"{label}引用了不存在的规格条目：{identity}")
        kind = row.get("kind")
        if constraint:
            if kind != "constraint":
                raise ValueError(f"{label}只能引用约束条目：{identity}")
        elif kind not in REQUIREMENT_KINDS:
            raise ValueError(f"{label}不能引用节点、依赖、里程碑或约束条目：{identity}")


def _milestone_scope(scope, milestone_id):
    return scope == milestone_id or scope == {"milestone_id": milestone_id}


def _milestone_references(key, row, entries):
    data = row.get("data") or {}
    requirements = _declared_ids(data, "goal_ids") + _declared_ids(data, "requirement_ids")
    constraints = _declared_ids(data, "constraint_ids")
    for identity, item in entries.items():
        if item.get("scope") != "global" and not _milestone_scope(item.get("scope"), key):
            continue
        if item.get("kind") == "constraint":
            constraints.append(identity)
        elif item.get("kind") in REQUIREMENT_KINDS:
            requirements.append(identity)
    return sorted(set(requirements)), sorted(set(constraints))


def compile_plan(flow):
    meta = flow.meta
    from mewcode.orchestration.parallel import flow_groups
    parallel_joins = {group.join_node_id: group for group in flow_groups(flow)}
    source = meta.get("collaboration_spec")
    if not isinstance(source, dict) or source.get("format") != FORMAT or not source.get("task_id"):
        raise ValueError("协作编排缺少任务规格及来源快照，请从任务设计入口保存。")
    planning = source.get("planning_mode", "eager")
    if planning == "rolling":
        rolling = True
    elif planning == "eager":
        rolling = False
    else:
        raise ValueError("协作规划模式无效，仅支持 eager 或 rolling。")
    rows = (source.get("document") or {}).get("entries", [])
    entries = {}
    for row in rows:
        if row.get("state") == "retired":
            continue
        if row.get("state") == "conflict":
            raise ValueError("协作规格仍有冲突，请先处理后执行。")
        from mewcode.kernel.constraints import annotate
        item = annotate(row, source)
        entries[row["id"]] = item
    if not entries:
        raise ValueError("协作规格没有可追溯条目")
    goals = [key for key, row in entries.items() if row["kind"] == "goal"]
    if not goals:
        raise ValueError("协作规格缺少原始目标")
    node_ids = {node.node_id for node in flow.nodes}
    parents = (meta.get("workflow_draft") or {}).get("parents") or {}
    contracts = {}
    declared_milestones = {key: row for key, row in entries.items() if row["kind"] == "milestone"}
    deferred = {}
    for key, row in declared_milestones.items():
        data = row.get("data") or {}
        if "deferred" not in data:
            continue
        if type(data["deferred"]) is not bool:
            raise ValueError("里程碑 deferred 标记必须为布尔值：" + key)
        if not data["deferred"]:
            continue
        if not rolling:
            raise ValueError("经典协作规划不支持远期里程碑：" + key)
        deferred[key] = row
    future_checks = {}
    if deferred:
        from mewcode.kernel.contracts import checks as declared_checks
        for key, row in deferred.items():
            data = row.get("data") or {}
            if not isinstance(row.get("body"), str) or not row["body"].strip():
                raise ValueError("远期里程碑必须有明确目标描述：" + key)
            goal_ids = _declared_ids(data, "goal_ids")
            if not goal_ids:
                raise ValueError("远期里程碑必须声明非空 goal_ids：" + key)
            _check_reference_types(goal_ids, entries, label="远期里程碑 goal_ids")
            for identity in goal_ids:
                if entries[identity]["kind"] != "goal":
                    raise ValueError("远期里程碑 goal_ids 必须引用原始目标：" + identity)
            _check_reference_types(_declared_ids(data, "requirement_ids"), entries,
                                   label="远期里程碑 requirement_ids")
            _check_reference_types(_declared_ids(data, "constraint_ids"), entries,
                                   constraint=True, label="远期里程碑 constraint_ids")
            raw_checks = data.get("checks", [])
            future_checks[key] = [] if raw_checks == [] else declared_checks(raw_checks)
    references = {key: _milestone_references(key, row, entries) for key, row in deferred.items()}
    for node in flow.nodes:
        key = node.node_id
        if node.members:
            raise ValueError("协作模式请将成员工作保存为独立合同节点，不能隐式沿用成员委派")
        brief = (meta.get("node_plan_briefs") or {}).get(key) or {}
        from mewcode.kernel.contracts import design_contract
        declared = design_contract((entries.get(key, {}).get("data") or {}).get("contract", {}))
        milestone = declared.get("milestone_id") or parents.get(key) or "milestone_1"
        if declared.get("milestone_id") and milestone not in declared_milestones:
            raise ValueError("合同引用的里程碑不存在")
        if rolling and milestone not in declared_milestones:
            raise ValueError("滚动计划中的近期合同必须属于已声明的里程碑")
        if milestone in deferred:
            raise ValueError("远期里程碑不能包含当前执行合同：" + milestone)
        if rolling:
            _check_reference_types(declared.get("requirement_ids", []), entries,
                                   label=f"合同「{key}」requirement_ids")
            _check_reference_types(declared.get("constraint_ids", []), entries,
                                   constraint=True, label=f"合同「{key}」constraint_ids")
        def applies(row):
            return row["scope"] in ("global", key, {"node_id": key}, {"milestone_id": milestone})
        requirement_ids = goals + [identity for identity, row in entries.items()
                                   if row["kind"] in {"artifact", "input", "acceptance", "decision", "assumption"} and applies(row)]
        constraint_ids = [identity for identity, row in entries.items()
                          if row["kind"] == "constraint" and applies(row)]
        requirement_ids = sorted(set(requirement_ids + declared.get("requirement_ids", [])))
        constraint_ids = sorted(set(constraint_ids + declared.get("constraint_ids", [])))
        criteria = list(brief.get("acceptance") or [])
        gate = (meta.get("node_kinds") or {}).get(key) == "gate"
        if not gate and not criteria:
            raise ValueError(f"工作单元「{node.label}」缺少验收标准，未把自述完成当作验收。")
        contracts[key] = {
            "id": key, "version": 1, "node_id": key, "goal": node.responsibility,
            "label": node.label, "milestone_id": milestone,
            "requirement_ids": requirement_ids, "constraint_ids": constraint_ids,
            "inputs": list(brief.get("known_inputs") or []), "deliverables": list(brief.get("outputs") or []),
            "forbidden_changes": ["不得修改用户目标、验收、授权或未批准的指定方法", *declared.get("forbidden_changes", [])],
            "capabilities": copy.deepcopy(node.capabilities), "tools": list(node.tools) if node.tools is not None else None,
            "deny_tools": list(node.deny_tools), "method": declared.get("method"), "method_decision": None,
            "depends_on": sorted({e.from_node for e in flow.edges if e.to_node == key and e.condition in {"always", "pass"}}),
            "checks": declared.get("checks") or [{"id": f"{key}_acceptance", "kind": "gate" if gate else "review", "version": 1,
                        "criteria": criteria or ["宿主关口已有有效决定"]}],
        }
        if key in parallel_joins:
            contracts[key]['parallel_group'] = parallel_joins[key].as_dict()
        if "workspace_mode" in declared:
            if node.kind != 'role' or node.members or gate:
                raise ValueError("隔离工作区只支持独立角色节点")
            if flow.meta.get('loops'):
                raise ValueError('隔离工作区需要独立无环合同，循环计划暂不支持工作区切换')
            contracts[key]['workspace_mode'] = declared['workspace_mode']
    covered = {key for contract in contracts.values() for key in contract["requirement_ids"] + contract["constraint_ids"]}
    if rolling:
        if not contracts:
            raise ValueError("协作滚动计划必须保留至少一个近期可执行合同。")
        covered |= {identity for requirement_ids, constraint_ids in references.values()
                    for identity in requirement_ids + constraint_ids}
    uncovered = [key for key, entry in entries.items() if entry["kind"] in {"goal", "input", "artifact", "acceptance", "constraint"}
                 and key not in covered]
    if uncovered:
        raise ValueError("有要求尚未安排完成位置：" + "、".join(uncovered))
    validate_contracts(contracts, entries, node_ids)
    milestones = {}
    for key in dict.fromkeys([*declared_milestones, *(c["milestone_id"] for c in contracts.values())]):
        row = declared_milestones.get(key, {})
        data = row.get("data") or {}
        milestone = {"id": key, "contracts": [c["id"] for c in contracts.values() if c["milestone_id"] == key],
                     "goal": row.get("body", ""),
                     "depends_on": _declared_ids(data, "depends_on") if rolling else copy.deepcopy(data.get("depends_on", [])),
                     "checks": copy.deepcopy(future_checks[key] if key in future_checks else data.get("checks", [])),
                     "status": "unexpanded" if key in deferred else "planned"}
        if rolling:
            from mewcode.kernel.capabilities import declaration
            from mewcode.kernel.contracts import text_list
            milestone.update(capabilities=declaration(data.get("capabilities", {})),
                             tools=text_list(data["tools"]) if "tools" in data else None,
                             deny_tools=text_list(data.get("deny_tools", [])),
                             inputs=text_list(data.get("inputs", [])),
                             outputs=text_list(data.get("outputs", [])))
            if key in deferred:
                milestone["goal_ids"] = _declared_ids(data, "goal_ids")
                milestone["requirement_ids"], milestone["constraint_ids"] = references[key]
            else:
                milestone["requirement_ids"] = sorted({identity for contract in milestone["contracts"]
                                                       for identity in contracts[contract]["requirement_ids"]})
                milestone["constraint_ids"] = sorted({identity for contract in milestone["contracts"]
                                                      for identity in contracts[contract]["constraint_ids"]})
        milestones[key] = milestone
    for milestone in milestones.values():
        if not milestone["contracts"]:
            if milestone["status"] == "unexpanded":
                continue
            raise ValueError("里程碑尚未安排工作合同：" + milestone["id"])
        milestone["depends_on"] = sorted(set(milestone["depends_on"]) | {
            contracts[parent]["milestone_id"] for key in milestone["contracts"]
            for parent in contracts[key]["depends_on"] if contracts[parent]["milestone_id"] != milestone["id"]})
    visited, active = set(), set()
    def visit_milestone(key):
        if key not in milestones:
            raise ValueError("里程碑依赖不存在：" + key)
        if key in active:
            raise ValueError("里程碑依赖不能成环")
        if key in visited:
            return
        active.add(key)
        for parent in milestones[key]["depends_on"]:
            visit_milestone(parent)
        active.remove(key)
        visited.add(key)
    for key in milestones:
        visit_milestone(key)
    final = flow.finalizer_node_id
    plan = {"format": ROLLING_FORMAT if rolling else FORMAT, "task_id": source["task_id"], "source_hash": digest(source),
            "source_snapshot": copy.deepcopy(source), "goal": source.get("goal", ""),
            "entries": copy.deepcopy(entries), "contracts": contracts, "final_contract_id": final,
            "milestones": milestones,
            "policy": {"max_attempts": 3, "max_model_calls": 60, "optional_questions_per_milestone": 2,
                       "max_review_calls": 600, "max_active_milestones": 2, "context_char_budget": 64000}}
    if rolling:
        plan["planning_mode"] = "rolling"
    return plan


def validate_contracts(contracts, entries, node_ids=None):
    if not contracts or len(contracts) > 200:
        raise ValueError("合同数必须介于 1 和 200")
    visited, active = set(), set()

    def visit(key):
        if key in active:
            raise ValueError("协作合同依赖不能成环")
        if key in visited:
            return
        active.add(key)
        contract = contracts[key]
        if contract.get("id") != key or type(contract.get("version")) is not int or contract["version"] < 1:
            raise ValueError("合同身份或版本无效")
        if node_ids is not None and contract.get("node_id") not in node_ids:
            raise ValueError("合同缺少可执行工作单元")
        if not contract.get("goal") or not contract.get("requirement_ids") or not contract.get("checks"):
            raise ValueError("合同必须包含目标、需求引用和验收标准")
        if set(contract.get("requirement_ids", []) + contract.get("constraint_ids", [])) - set(entries):
            raise ValueError("合同引用了不存在的规格条目")
        for parent in contract.get("depends_on", []):
            if parent not in contracts:
                raise ValueError("合同输入生产者不存在")
            visit(parent)
        active.remove(key)
        visited.add(key)

    for key in contracts:
        visit(key)
    from mewcode.orchestration.collab_workspaces import validate_workspace_contracts
    validate_workspace_contracts(contracts)
