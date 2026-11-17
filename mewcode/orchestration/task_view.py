"""Deterministic, read-only host projections for the task workbench.

This is a presentation adapter, not a scheduler or an additional fact store.
Unknown outcomes never become completed work; certificates remain authoritative.
"""
from __future__ import annotations

import copy

from mewcode.kernel import collaboration as ledger
from mewcode.kernel.events import digest

TASK_STATUSES = ("设计中", "等待开始", "进行中", "等你处理", "已暂停", "已完成", "已停止")
STEP_STATUSES = ("未开始", "进行中", "验收中", "等你处理", "需返工", "已完成", "已跳过", "已暂停", "已停止")
TASK_STATUS_MAP = {
    "designing": "设计中", "building": "设计中", "draft": "等待开始", "queued": "等待开始", "ready": "等待开始",
    "pending": "等待开始", "planned": "等待开始", "running": "进行中", "working": "进行中",
    "continuing": "进行中", "reviewing": "进行中", "checking": "进行中", "verifying": "进行中",
    "needs_input": "等你处理", "needs_attention": "等你处理", "awaiting_user": "等你处理",
    "waiting_permission": "等你处理", "waiting": "等你处理", "blocked": "等你处理", "intervening": "等你处理",
    "paused": "已暂停", "held": "已暂停", "completed": "已完成", "complete": "已完成",
    "terminated": "已停止", "cancelled": "已停止", "canceled": "已停止", "failed": "已停止",
    "stopped": "已停止", "budget_exhausted": "已停止", "unknown": "已停止", "stalled": "已停止",
}
STEP_STATUS_MAP = {
    "": "未开始", "pending": "未开始", "queued": "未开始", "planned": "未开始", "ready": "未开始",
    "running": "进行中", "working": "进行中", "dispatched": "进行中", "continuing": "进行中",
    "checking": "验收中", "reviewing": "验收中", "review_pending": "验收中", "verifying": "验收中",
    "waiting_permission": "等你处理", "awaiting_user": "等你处理", "needs_input": "等你处理",
    "blocked": "等你处理", "needs_attention": "等你处理", "unknown": "等你处理", "expired": "等你处理",
    "waiting_inputs": "未开始", "claimed": "进行中",
    "paused": "已暂停", "held": "等你处理", "rejected": "需返工", "invalidated": "需返工",
    "incomplete_audit": "需返工", "completed": "已完成", "accepted": "已完成",
    "skipped": "已跳过", "branch_not_taken": "已跳过", "terminated": "已停止",
    "failed": "已停止", "cancelled": "已停止", "canceled": "已停止", "stopped": "已停止",
    "budget_exhausted": "已停止", "stalled": "已停止", "superseded_closed": "已停止",
}
CHANGE_LABELS = frozenset({"要求修订", "换路", "重新规划", "约束变更", "假设", "人工判定通过", "协调者更换", "修订草稿未采用"})
LEGACY_TEXT = "这是旧格式的运行记录，只能查看"
POLICY_TEXT = "这是系统策略，不能在这里修改"


def task_status(value):
    return TASK_STATUS_MAP.get(str(value or ""), "已停止")


def team_header(snapshot):
    """Team storage is project-scoped; never invent a chat ownership binding."""
    team, run = snapshot['team'], snapshot['run']
    status = task_status(run.get('state'))
    if team.get('lifecycle') == 'suspended' and status not in {'已完成', '已停止'}:
        status = '已暂停'
    if team.get('lifecycle') == 'closed' and status != '已完成':
        status = '已停止'
    return dict(id='team:' + team['team_id'], run_id=run['team_run_id'], conversation_id='', scope='project',
                mode='team', title=team.get('name') or '团队任务', goal=run.get('objective') or '',
                status=status, spec_version=team.get('spec_version', 1), plan_version=run.get('task_set_revision', 1),
                control_epoch=team.get('control_epoch'), state_version=run.get('state_version'), team_state_version=team.get('state_version'),
                controls=['pause', 'cancel'] if status == '进行中' else (['cancel'] if status in {'等你处理', '已暂停'} else []),
                reason='', design_revision=None, updated_at='', current_activity=_activity(status, []))


def step_status(value):
    return STEP_STATUS_MAP.get(str(value or ""), "已停止")


def team_view(snapshot):
    """Project team snapshot -> the same workbench schema, without side effects."""
    task = team_header(snapshot)
    orchestration = snapshot.get('orchestration') or {}
    mission = orchestration.get('mission') or {}
    contract = mission.get('contract') or {}
    task['spec_version'] = mission.get('version', task['spec_version'])
    task['goal'] = contract.get('objective') or task['goal']
    view = dict(schema_version=1, task=task, legacy=False, legacy_message='', inbox=[], plan=[],
                deliverables=[], changes=[], requirements=[], structure={})
    assignments = {row['task_key']: row for row in mission.get('assignments', [])}
    roles = {row['role_id']: row.get('responsibility') or row['role_id'] for row in snapshot.get('definition', {}).get('roles', [])}
    tasks = {row['task_id']: row for row in snapshot.get('tasks', [])}
    histories = snapshot.get('histories') or {}
    groups, task_keys = {}, {}
    rows = list(tasks.values())
    if not rows:
        rows = [dict(task_id='planned:' + row['task_key'], task_key=row['task_key'], contract=row, state='planned')
                for row in snapshot.get('definition', {}).get('tasks', [])]
    for row in rows:
        detail = row.get('contract') or {}
        group_id = assignments.get(row['task_key'], {}).get('owner_role') or detail.get('role_id') or 'default'
        group = groups.setdefault(group_id, dict(id=group_id, title=roles.get(group_id, '任务计划'), steps=[]))
        task_keys[row['task_key']] = row['task_id']
        history = histories.get(row['task_id']) or {}
        current = next((item for item in history.get('revisions', []) if item['revision'] == row.get('revision')), {})
        reviews = current.get('reviews', [])
        accepted = bool(row.get('state') == 'completed' and reviews and reviews[-1].get('accepted'))
        status = step_status(row.get('state'))
        if row.get('block_reason') in {'review_rejected', 'artifact_revoked', 'input_rejected'}:
            status = '需返工'
        if row.get('state') == 'completed' and not accepted:
            status = '等你处理'
        group['steps'].append(dict(id=row['task_id'], name=detail.get('objective') or row['task_key'], status=status,
            attempts=row.get('attempts', 0), acceptance=dict(passed=True, summary='当前版本验收通过') if accepted else None,
            detail=dict(goal=detail.get('objective', ''), constraints=copy.deepcopy(detail.get('forbidden_changes', [])),
                        requirements=copy.deepcopy(detail.get('acceptance', [])), checks=copy.deepcopy(reviews),
                        method=copy.deepcopy(detail.get('verification')), history=copy.deepcopy(history)),
            detail_ref=dict(run_id=task['run_id'], task_id=row['task_id'], revision=row.get('revision', 1))))
    for group in groups.values():
        group['status'] = _group_status(group['steps'])
    view['plan'] = list(groups.values())
    required = {row['task_id'] for row in rows if row.get('contract', {}).get('required', True)}
    if task['status'] == '已完成' and any(step['status'] != '已完成' for group in view['plan'] for step in group['steps'] if step['id'] in required):
        task.update(status='等你处理', reason='相关交付尚无有效验收记录', controls=[])
    pending = {'pending', 'needs_clarification', 'received'}
    for row in orchestration.get('interventions', []):
        if row.get('state') not in pending or row.get('intent') in {'question', 'information'}:
            continue
        basis = dict(request=row.get('request'), receipt=row.get('receipt'), state=row.get('state'), epoch=task['control_epoch'])
        view['inbox'].append(dict(id='team_intervention:' + row['intervention_id'] + ':' + digest(basis),
            type='team_intervention', source_id=row['intervention_id'], title='确认这项任务变更',
            background=(row.get('request') or {}).get('message', ''), mandatory=True, deadline=None,
            run_id=task['run_id'], task_id=task['id'], source=copy.deepcopy(row), basis=basis))
    for row in orchestration.get('plan_proposals', []):
        if row.get('state') == 'proposed':
            view['inbox'].append(dict(id='team_plan:' + row['proposal_id'], type='team_plan', source_id=row['proposal_id'],
                title='确认新的团队计划', background=row.get('reason', ''), mandatory=True, deadline=None,
                run_id=task['run_id'], task_id=task['id'], source=copy.deepcopy(row),
                basis=dict(plan_version=task['plan_version'], spec_version=task['spec_version'], epoch=task['control_epoch'])))
    artifacts = {row['version_id']: row for row in snapshot.get('artifacts', [])}
    publications = {}
    for publication in snapshot.get('publications', []):
        for entry in publication.get('manifest', []):
            publications.setdefault(entry['version_id'], []).append(dict(id=publication['publication_id'],
                state=publication['state'], path=entry.get('relative_path', ''), time=publication.get('created_at', '')))
    for version_id, artifact in artifacts.items():
        producer = tasks.get(artifact.get('producer_task_id')) or {}
        accepted = any(handoff.get('state') == 'published' and handoff.get('revision') == producer.get('revision')
                       and any(item.get('version_id') == version_id for item in handoff.get('packet', {}).get('artifacts', []))
                       for handoff in producer.get('handoffs', []))
        current = bool(producer.get('state') == 'completed' and accepted and artifact.get('valid_for_consumption'))
        old_accepted = accepted or bool(publications.get(version_id)) or any(
            handoff.get('state') == 'published' and any(item.get('version_id') == version_id for item in handoff.get('packet', {}).get('artifacts', []))
            for row in [*snapshot.get('tasks', []), *snapshot.get('historical_tasks', [])] for handoff in row.get('handoffs', []))
        entries = publications.get(version_id) or [{}]
        for publication in entries:
            view['deliverables'].append(dict(id=version_id + ':' + publication.get('id', ''), node_id=artifact.get('producer_task_id', ''),
                name=publication.get('path') or artifact.get('port_id') or '团队交付物', kind='team_artifact',
                artifact_version=version_id, sha256=artifact.get('content_hash', ''), size=artifact.get('size'),
                status='已验收' if current else ('需重新核验' if old_accepted else '未通过'),
                reason='' if current else '当前版本尚无有效验收记录', publication=publication))
    labels = {'team_intervention_received': '要求修订', 'team_intervention_clarified': '要求修订',
              'team_intervention_applied': '要求修订', 'team_intervention_dismissed': '修订草稿未采用',
              'team_plan_proposed': '重新规划', 'team_plan_replaced': '重新规划', 'team_plan_amended': '重新规划',
              'team_plan_dismissed': '修订草稿未采用'}
    intervention_ids = {row['intervention_id'] for row in orchestration.get('interventions', [])}
    proposal_ids = {row['proposal_id'] for row in orchestration.get('plan_proposals', [])}
    for event in snapshot.get('events', []):
        kind = labels.get(event.get('event_type'))
        payload = event.get('payload') or {}
        event_run = payload.get('team_run_id') or payload.get('run_id')
        belongs = event_run == task['run_id'] or (not event_run and (
            payload.get('intervention_id') in intervention_ids or payload.get('proposal_id') in proposal_ids or payload.get('task_id') in tasks))
        if kind and belongs:
            view['changes'].append(dict(id=str(event['sequence']), type=kind, time=event.get('created_at', ''),
                summary=payload.get('reason') or kind, detail=copy.deepcopy(payload)))
    if contract:
        view['requirements'] = [dict(id='goal', kind='goal', body=task['goal'], editable=False, edit_reason='请通过团队变更提出修订')]
        view['requirements'] += [dict(id='constraint:' + str(i), kind='constraint', body=value, strength='hard', origin='user',
                                     editable=False, edit_reason='请通过团队变更提出修订') for i, value in enumerate(contract.get('hard_constraints', []))]
    edges = [dict(**{'from': task_keys[edge['source']['task_key']], 'to': task_keys[edge['target']['task_key']]}, condition='always')
             for edge in snapshot.get('definition', {}).get('connections', [])
             if edge['source'].get('task_key') in task_keys and edge['target'].get('task_key') in task_keys]
    _structure(view, edges)
    if view['inbox'] and task['status'] in {'进行中', '等待开始'}:
        task.update(status='等你处理', current_activity='有事项需要你处理')
    return finish(view)


def identity(record, design=None):
    """Historical runs keep their own task binding, never the current design's."""
    flow = record.get("locked_flow") or {}
    own = (record.get("collaboration") or {}).get("task_id") or (flow.get("meta", {}).get("task_design") or {}).get("task_id")
    if own:
        return str(own)
    if design and design.get("run_id") == record.get("run_id"):
        return str(design["task_id"])
    return "run:" + str(record.get("run_id", ""))


def is_legacy(record):
    return bool(record.get("legacy") or record.get("migration_required") or "kernel_revision" not in record)


def finish(view):
    """Content hashes exclude the hash itself and all wall-clock observations."""
    from mewcode.expert_runs import redact_for_display
    from mewcode.unicode_text import utf8_safe_json
    result = utf8_safe_json(redact_for_display(view))
    align_execution_display(result)
    # Provider IDs in typed model references are public catalog identifiers.
    # Keep generic secret redaction for every other field in the draft.
    from mewcode.model_registry import ModelRef
    originals = {row.get('id'): row for row in view.get('requirements', []) if row.get('kind') == 'node'}
    for row in result.get('requirements', []):
        model = originals.get(row.get('id'), {}).get('data', {}).get('model')
        if model:
            try:
                row.setdefault('data', {})['model'] = ModelRef.from_value(model).as_dict()
            except ValueError:
                pass
    result.pop("version", None)
    result["version"] = digest(result)
    return result


def align_execution_display(view):
    """A retained dispatch is progress history, not proof of a live call.

    Also applies to old materialized views at read time, without rebuilding or
    modifying the run journal. Parallel work on a running task stays visible.
    """
    task = view.get('task') or {}
    if not task.get('run_id') or task.get('design_stage'):
        return
    if task.get('mode') == 'workflow':
        from mewcode.workflow_core.display import align
        align(view)
        return
    execution = task.get('execution_status') or task.get('status')
    workflow_blocked = task.get('mode') == 'workflow' and execution == 'blocked'
    if view.get('inbox_resolved'):
        needs_user = any(row.get('mandatory', True) for row in view.get('inbox', []))
        task['waiting_for'] = 'user' if needs_user else 'system'
        if not needs_user and execution in {'blocked', 'needs_attention', 'waiting_permission'} and not workflow_blocked:
            task['status'] = '已暂停'
            task['controls'] = _controls('已暂停', view.get('legacy', False))
            for group in view.get('plan', []):
                for step in group.get('steps', []):
                    if step.get('status') == '等你处理':
                        step['status'] = '已暂停'
                group['status'] = _group_status(group.get('steps', []))
    if execution == 'running':
        # A branch question does not stop the host or its independent work.
        # The same execution fact owns the header, toolbar and controls.
        task['status'] = '进行中'
        task['controls'] = _controls('进行中', view.get('legacy', False))
        task['current_activity'] = _activity('进行中', view.get('plan', []))
        waiting = [step for group in view.get('plan', []) for step in group.get('steps', [])
                   if step.get('status') == '等你处理']
        task['waiting_branches'] = len(waiting)
        if waiting:
            task['current_activity'] += f'；{len(waiting)} 个分支待处理'
        if task.get('blocked_branches'):
            task['current_activity'] += f'；{task["blocked_branches"]} 个节点已阻塞'
        return
    if workflow_blocked:
        task.update(status='已阻塞', controls=_controls('已阻塞', view.get('legacy', False)))
    label = ('已阻塞' if workflow_blocked else '已暂停' if execution in {'paused', 'held', '已暂停'} else
             ('已暂停' if task.get('waiting_for') == 'system' else '等你处理')
             if execution in {'blocked', 'needs_attention', 'waiting_permission'} else
             '已停止' if execution in {'failed', 'cancelled', 'canceled', 'terminated', 'stopped',
                                      'budget_exhausted', 'unknown', 'stalled', '已停止'} else '')
    if not label:
        return
    changed = {}
    for group in view.get('plan', []):
        for step in group.get('steps', []):
            if (step.get('status') in {'进行中', '验收中'} or step.get('execution_suspended')
                    or step.get('status') == label == '已暂停'):
                step['status'] = label
                step['execution_suspended'] = True
                step['status_reason'] = task.get('reason') or {
                    '已暂停':'流程已暂停，当前节点进度已保存。',
                    '已阻塞':'对话没有继续推进，已有进度保留。',
                    '等你处理':'流程正在等待处理，当前节点尚未继续执行。',
                    '已停止':'流程已停止，当前节点的执行记录已保留。'}[label]
                changed[step['id']] = step
        if group.get('status') in {'进行中', '验收中'}:
            group['status'] = label
    for node in view.get('structure', {}).get('nodes', []):
        if node.get('id') in changed:
            step = changed[node['id']]
            node.update(status=step['status'], status_reason=step['status_reason'])
    task['current_activity'] = task.get('reason') if workflow_blocked and task.get('reason') else _activity(
        task.get('status'), view.get('plan', []), legacy=view.get('legacy', False))


def _activity(status, plan, *, reason="", legacy=False):
    if legacy:
        return LEGACY_TEXT
    for label, prefix in (("验收中", "正在核验："), ("进行中", "正在进行：")):
        names = [step.get("name") or "当前步骤" for group in plan for step in group.get("steps", [])
                 if step.get("status") == label]
        if names and status == "进行中":
            return prefix + "、".join(names[:3])
    return {"设计中": "正在整理任务目标与要求", "等待开始": "计划已记录，等待开始",
            "等你处理": "有事项需要你处理", "已暂停": "已保留进度，任务暂停中", "已阻塞": "对话没有继续推进，已有进度保留",
            "已完成": "任务已完成", "已停止": "任务已停止"}.get(status, "正在推进当前计划")


def _controls(status, legacy):
    if legacy:
        return []
    if status == "进行中":
        return ["pause", "cancel"]
    if status in {"等你处理", "已暂停", "已停止", "已阻塞"}:
        return ["resume", "cancel"]
    return []


def _base(record, mode, design=None):
    from mewcode.experts.outcome import effective_run_status
    legacy = is_legacy(record)
    status = task_status(effective_run_status(record))
    task_id = identity(record, design)
    title = str((record.get("locked_flow") or {}).get("label") or record.get("task") or "任务")
    task = dict(id=task_id, run_id=record.get("run_id", ""), conversation_id=record.get("conversation_id", ""),
                mode=mode, title=title, goal=str(record.get("task") or ""), status=status,
                spec_version=0, plan_version=1, controls=_controls(status, legacy),
                reason=str(record.get("status_reason") or ""), execution_status=record.get('status', ''), design_revision=None,
                updated_at=record.get("updated_at", ""), kernel_revision=record.get('kernel_revision', 0),
                run_attempt=record.get('attempt', 1), stage_cursor=record.get('stage_cursor', ''))
    if design and design.get("task_id") == task_id and design.get("run_id") == task["run_id"]:
        task["design_revision"] = design.get("revision")
    return dict(schema_version=1, task=task, legacy=legacy, legacy_message=LEGACY_TEXT if legacy else "",
                inbox=[], plan=[], deliverables=[], changes=[], requirements=[],
                structure=dict(nodes=[], edges=[], groups=[], versions=[]))


def _edges(flow):
    return [{"from": str(row.get("from_node") or row.get("from") or ""),
             "to": str(row.get("to_node") or row.get("to") or ""), "condition": str(row.get("condition") or "always")}
            for row in flow.get("edges", [])]


def _structure(view, edges, versions=None):
    nodes = [dict(id=step["id"], label=step["name"], status=step["status"], milestone_id=group["id"])
             for group in view["plan"] for step in group["steps"]]
    groups = [dict(id=group["id"], label=group["title"]) for group in view["plan"]]
    view["structure"] = dict(nodes=nodes, edges=edges, groups=groups, versions=versions or [
        dict(version=view["task"]["plan_version"], nodes=copy.deepcopy(nodes), edges=copy.deepcopy(edges))])
    view["task"]["current_activity"] = _activity(view["task"]["status"], view["plan"], legacy=view["legacy"])


def _group_status(steps):
    labels = {row["status"] for row in steps}
    if labels and labels <= {"已完成", "已跳过"}:
        return "已完成"
    return next((label for label in ("等你处理", "验收中", "进行中", "已阻塞", "需返工", "已暂停", "已停止") if label in labels), "未开始")


def classic_view(record, *, design=None, live=None):
    view = _base(record, "classic", design)
    flow = record.get("locked_flow") or {}
    states = record.get("node_states") or record.get("stages") or {}
    if live and live.get("run_id") == record.get("run_id") and live.get("conversation_id") == record.get("conversation_id"):
        states = live.get("node_states") or states
    nodes = flow.get("nodes") or [dict(node_id=key, label=row.get("label", key)) for key, row in states.items()]
    briefs = flow.get("meta", {}).get("node_plan_briefs", {})
    steps = []
    for node in nodes:
        key = str(node.get("node_id") or node.get("id") or "")
        state = states.get(key) or {}
        status = step_status(state.get("status"))
        if state.get("pending_interaction") or state.get("pending_permission"):
            status = "等你处理"
        checks = state.get("acceptance_checks") or []
        summary = None  # A completed node is not an acceptance certificate.
        detail = dict(goal=str(node.get("responsibility") or ""), constraints=[],
                      requirements=copy.deepcopy(briefs.get(key, {}).get("acceptance", [])),
                      checks=copy.deepcopy(checks), method=None, artifacts=[])
        steps.append(dict(id=key, name=str(node.get("label") or key), status=status,
                          attempts=int(state.get("node_attempt") or state.get("attempt") or 0),
                          acceptance=summary, detail=detail,
                          detail_ref=dict(run_id=record.get("run_id", ""), node_id=key)))
    view["plan"] = [dict(id="default", title="任务计划", status=_group_status(steps), steps=steps)]
    view["task"]["spec_version"] = int(record.get("flow_version") or flow.get("version") or 1)
    if not view["legacy"]:
        for row in record.get("process_file_receipts") or record.get("process_files") or []:
            view["deliverables"].append(dict(id=str(row.get("id") or digest(row)), node_id=row.get("node_id", ""),
                name=str(row.get("name") or "交付文件"), kind="file", sha256=row.get("sha256", ""),
                size=row.get("size"), file_id=row.get("id"), status="已验收" if row.get("accepted") is True else "未通过",
                reason="" if row.get("accepted") is True else "尚无有效验收记录"))
    else:
        for step in steps:
            step.update(detail=None, detail_ref=None, acceptance=None)
    _structure(view, _edges(flow) if not view["legacy"] else [])
    if not view['legacy']:
        from mewcode.experts.work_reporting import project_work_board
        project_work_board(view, record, flow)
    if view["legacy"]:
        view["structure"] = dict(nodes=[], edges=[], groups=[], versions=[])
    return finish(view)


def _requirements(value):
    result = []
    for row in value["entries"].values():
        if row["kind"] not in {"goal", "constraint", "acceptance", "decision", "assumption"}:
            continue
        result.append(dict(id=row["id"], title=str(row.get("title") or "要求"), kind=row["kind"],
                           body=str(row.get("body") or ""), field="body", editable=row.get("origin") != "host",
                           edit_reason=POLICY_TEXT if row.get("origin") == "host" else "",
                           sources=copy.deepcopy(row.get("field_sources") or row.get("sources") or []),
                           strength=row.get("strength"), origin=row.get("origin"), scope=copy.deepcopy(row.get("scope"))))
    for key, contract in value["contracts"].items():
        checks = contract.get('checks', [])
        if key in value['entries'] and len(checks) == 1 and checks[0].get('kind') == 'review':
            entry = value['entries'][key]
            editable = entry.get('origin') != 'host'
            result.append(dict(id=key, title='验收标准：' + contract['label'], kind='acceptance', field='acceptance',
                body='\n'.join(checks[0]['criteria']), editable=editable,
                edit_reason='' if editable else POLICY_TEXT,
                sources=copy.deepcopy((entry.get('field_sources') or {}).get('data.acceptance')
                                      or entry.get('sources') or [])))
        if contract.get("method") and key in value["entries"]:
            editable = value["entries"][key].get("origin") != "host"
            result.append(dict(id=key, title="指定方法：" + contract["label"], kind="method", field="method",
                               body=contract["method"]["description"], editable=editable,
                               edit_reason="" if editable else POLICY_TEXT, sources=copy.deepcopy(value["entries"][key].get("sources", []))))
    for entry in result:
        related = [value['proposals'][row['proposal_id']] for row in value.get('timeline', [])
                   if row['kind'] == 'proposal_recorded' and row.get('proposal_id') in value['proposals']
                   and entry['id'] in value['proposals'][row['proposal_id']]['payload']['changes']]
        if related:
            latest = related[-1]
            replacement = latest["payload"]["changes"][entry["id"]]
            pending_body = (replacement.get("data", {}).get("contract", {}).get("method", {}).get("description", entry["body"])
                            if entry["field"] == "method" else replacement.get("body", entry["body"]))
            if entry['field'] == 'acceptance':
                pending_body = '\n'.join(replacement.get('data', {}).get('acceptance', [])) or entry['body']
            entry["revision"] = dict(id=latest["id"], status={"proposed": "已记录", "needs_attention": "等待生效",
                "adopted": "已生效"}.get(latest["status"], "已记录"),
                pending_body=pending_body)
    return result


def _checks(contract, report):
    from mewcode.kernel.contracts import blocking_check
    actual = {row["id"]: row for row in (report or {}).get("checks", [])
              if isinstance(row, dict) and isinstance(row.get("id"), str)}
    return [dict(id=row["id"], name="；".join(row.get("criteria") or []) or "验收检查",
                 kind=row.get("kind", ""), version=row.get("version", 1),
                 passed=actual.get(row["id"], {}).get("passed"),
                 deferred=actual.get(row['id'], {}).get('deferred', False), blocking=blocking_check(row),
                 evidence=copy.deepcopy(actual.get(row["id"], {}).get("evidence", {})))
            for row in contract.get("checks", [])]


def _collab_changes(value):
    result = []
    for row in value.get("timeline", []):
        kind = row["kind"]
        proposal = value.get("proposals", {}).get(row.get("proposal_id"), {})
        payload = proposal.get("payload", {})
        label = {"coordinator_changed": "协调者更换", "human_acceptance": "人工判定通过",
                 "revision_draft_rejected": "修订草稿未采用", "revision_draft_not_revision": "修订草稿未采用",
                 "assumption_recorded": "假设", "plan_replaced": "重新规划", "method_changed": "换路"}.get(kind)
        if kind in {"proposal_recorded", "proposal_adopted"}:
            entries = list(payload.get("changes", {}).values())
            label = ("约束变更" if any(entry.get("kind") == "constraint" for entry in entries)
                     else "换路" if any(entry.get("kind") == "node" for entry in entries)
                     else "假设" if entries and all(entry.get("kind") == "assumption" for entry in entries)
                     else "要求修订")
        if not label:
            continue
        reason = row.get("reason") or ("已生效" if kind == "proposal_adopted" else "已记录，等待安全切换" if kind == "proposal_recorded" else "")
        result.append(dict(id=str(row["sequence"]), label=label, reason=str(reason),
                           actor=str(row.get("actor") or payload.get("actor") or ("user" if kind == "human_acceptance" else "host")),
                           time=row.get("time") or row.get("created_at") or "", sequence=row["sequence"],
                           evidence=copy.deepcopy(row), proposal_id=row.get("proposal_id")))
    return result


def _plan_versions(record, value, edges):
    """Rebuild semantic plan versions from journaled source and adopted proposals."""
    from mewcode.orchestration.collab_compile import compile_plan
    from mewcode.experts.flow import ExpertFlowSpec
    original = compile_plan(ExpertFlowSpec.from_value(record["locked_flow"]))
    contracts = copy.deepcopy(original["contracts"])
    entries = copy.deepcopy(original["entries"])
    versions = []
    edges = _edges(record["locked_flow"])

    def add(version):
        nodes = [dict(id=key, label=row["label"], goal=row["goal"], milestone_id=row["milestone_id"],
                      method=copy.deepcopy(row.get("method")), checks=copy.deepcopy(row.get("checks")),
                      requirement_ids=list(row.get("requirement_ids", [])), constraint_ids=list(row.get("constraint_ids", [])),
                      requirements=[dict(title=entries.get(identity, {}).get('title') or '要求',
                                         body=entries.get(identity, {}).get('body') or '')
                                    for identity in dict.fromkeys(row.get('requirement_ids', []) + row.get('constraint_ids', []))],
                      requirements_hash=digest({identity: entries.get(identity) for identity in
                                               row.get("requirement_ids", []) + row.get("constraint_ids", [])}))
                 for key, row in contracts.items()]
        versions.append(dict(version=version, nodes=nodes, edges=copy.deepcopy(edges)))

    add(1)
    for row in value.get("timeline", []):
        if row["kind"] == "graph_published":
            publication = value.get("graph_publications", {}).get(row.get("publication_id"))
            if publication and publication.get("status") == "published":
                contracts.update(copy.deepcopy(publication["fragment"]["contracts"]))
                edges = _edges(publication["fragment"]["flow"])
                add(publication["new_plan_version"])
            continue
        if row["kind"] != "proposal_adopted":
            continue
        proposal = value["proposals"].get(row.get("proposal_id"), {})
        for key, replacement in proposal.get("payload", {}).get("changes", {}).items():
            entries[key] = copy.deepcopy(replacement)
            if key in contracts and replacement.get("kind") == "node":
                updates = copy.deepcopy(replacement.get("data", {}).get("contract", {}))
                if 'checks' in updates and updates['checks'] != contracts[key]['checks']:
                    from mewcode.kernel.contracts import revised_acceptance_goal
                    contracts[key]['goal'] = revised_acceptance_goal(contracts[key], updates['checks'])
                contracts[key].update(updates)
        add(int(row.get("plan_version", versions[-1]["version"] + 1)))
    if versions[-1]["version"] != value["plan_version"]:
        contracts = copy.deepcopy(value["contracts"])
        add(value["plan_version"])
    return versions


def ordered_milestones(value):
    """Dependency order survives JSON object-key sorting during persistence."""
    rows, ordered, seen = value['milestones'], [], set()
    def visit(key):
        if key in seen or key not in rows:
            return
        seen.add(key)
        for dependency in rows[key].get('depends_on', []):
            visit(dependency)
        ordered.append(rows[key])
    for key in rows:
        visit(key)
    return ordered


def _budget_summary(value, lineage_id=None, *, book=None):
    from mewcode.kernel import lineage_budget
    book = book if book is not None else lineage_budget.ensure(copy.deepcopy(value))
    counts = book["root"] if lineage_id is None else book["lineages"].get(lineage_id, {})
    scope = "root" if lineage_id is None else "lineage"
    rows = [{"kind": kind, "label": label, "used": counts.get("used", {}).get(kind, 0),
             "reserved": counts.get("reserved", {}).get(kind, 0),
             "limit": book["limits"][scope][kind] + sum(row["amount"] for row in book["grants"].values()
                 if row["kind"] == kind and row["lineage_id"] == lineage_id)}
            for kind, label in (("attempts", "执行尝试"), ("revisions", "计划修订"), ("expansions", "任务拆分"))]
    if lineage_id is not None and lineage_budget.elastic(value):
        for row in rows:
            kind = row['kind']
            total = book['limits']['root'][kind] + sum(grant['amount'] for grant in book['grants'].values()
                if grant['kind'] == kind and grant['lineage_id'] is None)
            row.update(allocation_mode='elastic', nominal_limit=row['limit'], limit=None,
                shared_remaining=max(0, total - book['root']['used'][kind] - book['root']['reserved'][kind]))
    return rows


def collaborative_view(record, *, design=None):
    value = ledger.state(record)  # Invalid collaborative data never falls back to classic.
    view = _base(record, "collaborative", design)
    if view["legacy"]:
        legacy = classic_view(record, design=design)
        legacy["task"]["mode"] = "collaborative"
        return finish(legacy)
    view["task"].update(goal=value["goal"], spec_version=value["spec_version"], plan_version=value["plan_version"])
    view['task']['revision_available'] = bool(design and design.get('task_id') == value['task_id']
                                             and design.get('run_id') == record['run_id'])
    view["requirements"] = _requirements(value)
    from mewcode.kernel import lineage_budget
    budget_book = lineage_budget.ensure(copy.deepcopy(value))
    view["task"]["budget"] = _budget_summary(value, book=budget_book)
    recovery = collaboration_projection(record)["recovery_gates"]
    waiting = {key for gate in recovery for key in gate["node_ids"]}
    stages = record.get("node_states") or {}
    final_checked = (ledger.final_ready(value)
                     and value['integrations']['final']['report'].get('passed') is True)
    for milestone in ordered_milestones(value):
        steps = []
        for key in milestone["contracts"]:
            contract = value["contracts"][key]
            attempts = sorted((a for a in value["attempts"].values() if a["contract_id"] == key), key=lambda a: a["number"])
            latest = attempts[-1] if attempts else {}
            certificate = value["certificates"].get(key)
            valid = ledger.valid_certificate(value, key)
            report = latest.get("report") or {}
            if valid:
                report = value['attempts'].get(certificate['attempt_id'], {}).get('report') or report
            status = "已完成" if valid else step_status(latest.get("status", stages.get(key, {}).get("status")))
            if not valid and certificate:
                status = "需返工" if status not in {"进行中", "验收中"} else status
            if key in waiting or contract.get("held") or stages.get(key, {}).get("pending_interaction"):
                status = "等你处理"
            summary = None
            if certificate or report:
                checks = report.get("checks", [])
                summary = dict(status=("集中复核通过" if final_checked else "已验收" if certificate.get('quality_passed', True) else "可交接 · 质量待复核")
                               if valid else "需重新核验" if certificate else "未通过",
                               passed=sum(row.get("passed") is True for row in checks), total=len(contract["checks"]))
            detail = dict(goal=contract["goal"], constraints=[copy.deepcopy(row) for row in view["requirements"] if row["id"] in contract["constraint_ids"]],
                          requirements=[copy.deepcopy(row) for row in view["requirements"] if row["id"] in contract["requirement_ids"]],
                          checks=_checks(contract, report), method=copy.deepcopy(contract.get("method")),
                          artifacts=copy.deepcopy((certificate or report).get("artifacts", [])),
                          forbidden_changes=list(contract.get("forbidden_changes", [])),
                          workspace=copy.deepcopy(latest.get('workspace')),
                          expanded_from=copy.deepcopy(contract.get("expanded_from")),
                          parent_label=(value["contracts"].get((contract.get("expanded_from") or {}).get("contract_id")) or {}).get("label"),
                          join_children=[dict(id=child, label=value["contracts"][child]["label"])
                                         for child in contract.get("join_children", [])],
                          lineage_id=contract.get("lineage_id") or key,
                          budget=_budget_summary(value, contract.get("lineage_id") or key, book=budget_book))
            if valid and final_checked:
                for check in detail['checks']:
                    if check.get('deferred'):
                        check['reviewed'] = True
            steps.append(dict(id=key, name=contract["label"], status=status, attempts=len(attempts), acceptance=summary,
                              detail=detail, detail_ref=dict(run_id=record["run_id"], node_id=key)))
            # Keep every accepted/rejected artifact version from attempt reports;
            # a replacement certificate must not erase an earlier delivery.
            for attempt in attempts:
                for index, artifact in enumerate(attempt.get("report", {}).get("artifacts", [])):
                    current = bool(certificate and certificate.get("attempt_id") == attempt["id"])
                    accepted = attempt.get("status") == "accepted"
                    delivery_status = ("已验收" if final_checked or certificate.get('quality_passed', True) else "可用初稿") if current and valid else "需重新核验" if accepted else "未通过"
                    receipt = next((r for r in record.get("process_file_receipts", [])
                                    if r.get("sha256") == artifact.get("sha256") and r.get("node_id") == key), {})
                    view["deliverables"].append(dict(id=digest([attempt["id"], index, artifact.get("sha256")]),
                        node_id=key, attempt_id=attempt["id"], name=artifact.get("name") or str(artifact.get("path", "")).replace("\\", "/").rsplit("/", 1)[-1] or "交付物",
                        kind=artifact.get("kind", "file"), sha256=artifact.get("sha256", ""), size=artifact.get("size"),
                        status=delivery_status, reason="" if delivery_status == "已验收" else "待集中复核或修订" if delivery_status == '可用初稿'
                        else "相关版本已变化" if accepted else "尚未满足交接条件",
                        file_id=('collab:' + digest([attempt['id'], index, artifact.get('sha256')])) if artifact.get('snapshot_path') else receipt.get("id"),
                        inline_content=artifact.get("content") if artifact.get("kind") == "inline" else None))
        group_status = _group_status(steps)
        if group_status == "已完成" and milestone.get("status") not in {"completed", "accepted"}:
            group_status = "验收中"
        if milestone.get("status") == "unexpanded":
            group_status = "待展开"
        view["plan"].append(dict(id=milestone["id"], title=milestone.get("goal") or "当前里程碑",
            status=group_status, steps=steps, deferred=milestone.get("status") == "unexpanded",
            requirements=list(milestone.get("requirement_ids", [])), checks=copy.deepcopy(milestone.get("checks", []))))
    if recovery or value.get("blocked"):
        view["task"]["status"] = "等你处理"
    if view["task"]["status"] == "已完成" and (any(group["deferred"] for group in view["plan"])
            or any(step["status"] not in {"已完成", "已跳过"} for group in view["plan"] for step in group["steps"])):
        view["task"]["status"] = "等你处理"
        view["task"]["reason"] = "相关交付尚无有效验收记录"
    view["task"]["controls"] = _controls(view["task"]["status"], False)
    view["changes"] = _collab_changes(value)
    from mewcode.kernel.collaboration_graph import effective_flow
    edges = _edges(effective_flow(record).as_dict())
    _structure(view, edges, _plan_versions(record, value, edges))
    from mewcode.experts.work_reporting import project_work_board
    project_work_board(view, record)
    final_report = value.get('integrations', {}).get('final', {}).get('report') or {}
    if view['task']['status'] == '已完成' and final_report and final_report.get('passed') is not True:
        view['task'].update(quality_status='needs_revision', current_activity='初稿已完成，仍有待修订项',
                            reason='各阶段已完成交接；集中复核发现的问题已保留，可继续定向修订。')
    elif view['task']['status'] == '已完成' and final_report.get('passed') is True:
        view['task']['quality_status'] = 'verified'
    return finish(view)


def from_record(record, *, design=None, live=None):
    if record.get('workflow_core'):
        from mewcode.workflow_core.projection import view
        return view(record, design=design)
    if record.get("orchestration_mode") == "collaborative" or record.get("collaboration"):
        return collaborative_view(record, design=design)
    return classic_view(record, design=design, live=live)


def design_view(design, *, conversation_id, entries=None):
    entries = [row for row in (entries if entries is not None else
               (design.get("specification") or {}).get("entries", [])) if row.get("state") != "retired"]
    status = design.get("status")
    controls = []
    if status in {"paused", "failed", "blocked", "needs_input"}:
        controls.append("resume")
    elif status in {"designing", "queued", "building"} or (status == "ready" and design.get("intent") == "execute"):
        controls.append("pause")
    if design.get("intent") == "execute" and status != "completed":
        controls.append("set_intent")
    task = dict(id=design["task_id"], run_id="", conversation_id=conversation_id,
                mode='workflow' if design.get('workflow_format') else design.get("orchestration_mode", "classic"), title=design.get("goal") or "一起完成任务",
                goal=design.get("goal", ""), status=task_status(design.get("status")), spec_version=design["revision"],
                plan_version=0, design_revision=design["revision"], intent=design.get("intent", "plan"),
                controls=controls, design_stage=True, reason=design.get("reason", ""), updated_at=design.get("updated_at", ""))
    health = design.get("health") or {}
    if health:
        # Include diagnostic changes in the view version even when the durable
        # task revision did not change (for example, a validator update).
        task["diagnosis_fingerprint"] = health["fingerprint"]
        if (health.get("next_action") in {"reload_runtime", "repair_current_materials"}
                or (health.get("previous_failure") or {}).get("status") == "historical"):
            task["reason"] = health["summary"]
    view = dict(schema_version=1, task=task, legacy=False, legacy_message="", inbox=[], plan=[],
                deliverables=[], changes=[], requirements=copy.deepcopy(list(entries)),
                structure=dict(nodes=[], edges=[], groups=[], versions=[]))
    # A draft preview reads the same entries used by construction. It does not
    # compile, validate, repair or create runtime evidence just to draw the graph.
    node_entries = [row for row in entries if row.get("kind") == "node"]
    node_ids = {row["id"] for row in node_entries}
    groups = {row["id"]: row.get("title") or row.get("body") or row["id"]
              for row in entries if row.get("kind") == "milestone"}
    steps_by_group = {}
    for row in node_entries:
        data = row.get("data") or {}
        group = data.get("milestone_id") or data.get("parent_id") or "default"
        related = [item for item in entries if (item.get("data") or {}).get("node_id") in {None, "", row["id"]}]
        detail = dict(goal=row.get("body", ""), inputs=copy.deepcopy(data.get("inputs", [])),
                      outputs=copy.deepcopy(data.get("outputs", [])),
                      requirements=copy.deepcopy(data.get("acceptance", [])),
                      constraints=copy.deepcopy([item for item in related if item.get("kind") == "constraint"]),
                      sources=copy.deepcopy(row.get("sources", [])), checks=[], artifacts=[])
        steps_by_group.setdefault(group, []).append(dict(id=row["id"], name=row.get("title") or row["id"],
            status="待运行" if status == "ready" else "草稿", attempts=0, acceptance=None, detail=detail, detail_ref=None))
    view["plan"] = [dict(id=key, title=groups.get(key, "任务计划"), status="草稿", steps=steps)
                    for key, steps in steps_by_group.items()]
    edges = [dict(id=row["id"], **{"from": row["data"].get("from_node", ""),
                  "to": row["data"].get("to_node", ""), "condition": row["data"].get("condition", "always"),
                  **{k: copy.deepcopy(row['data'][k]) for k in ('input_when', 'branch_group') if k in row['data']}})
             for row in entries if row.get("kind") == "edge" and row.get("data")]
    _structure(view, edges)
    by_id = {row['id']: row for row in node_entries}
    for node in view['structure']['nodes']:
        node['kind'] = by_id[node['id']].get('data', {}).get('node_kind', 'implement')
    view["structure"]["versions"] = []
    view["structure"]["phase"] = "saved" if status == "ready" else "design"
    view["structure"]["unresolved_edges"] = sum(edge["from"] not in node_ids or edge["to"] not in node_ids for edge in edges)
    return finish(view)


def collaboration_projection(record):
    value = ledger.state(record)
    labels = {"initialized": "规格已保存", "dispatched": "工作已派发", "accepted": "工作验收通过",
              "rejected": "工作验收未通过", "proposal_recorded": "已收到规格修订", "proposal_adopted": "规格修订已生效",
              "acceptance_invalidated": "相关交付需要重新核验", "integration_accepted": "集成验收通过",
              "integration_rejected": "集成验收未通过", "coordination_started": "协调者正在处理",
              "coordination_finished": "协调处理完成", "revision_waiting": "规格修订等待处理",
              "stale_delivery": "已保留旧版本回执", "coordinator_changed": "协调者已更换"}
    labels.update(recovery_gate_opened="需要选择恢复方式", retry_authorized="用户已明确允许重试",
                  human_acceptance="用户人工判定通过", integration_rework="正在返工集成交付",
                  infrastructure_recovery="正在恢复执行环境或模型服务", integration_unavailable="集成验收服务未完成")
    labels.update(verifier_model_changed="当前步骤的验收模型已更换",
                  execution_evidence_recheck="正在用宿主执行回执复核原交付")
    labels.update(delivery_ready="产物已可交接，质量检查留待集中复核", delivery_retained="已复用现有产物",
                  draft_reviewed="初稿已复核，待修订项已保留", execution_checkpoint_error="执行检查点需修复",
                  coordination_error="协调服务内部错误已记录")
    labels.update(graph_prepared="子任务划分正在复核", graph_published="子任务划分已生效",
                  graph_rejected="子任务划分未通过复核", budget_granted="用户已追加执行额度",
                  diagnostic_event="已归集本次失败证据")
    from mewcode.kernel.collaboration_recovery import gate_current, manual_candidate, unresolved_calls, verifier_switch_attempt, execution_recheck_gates, unstarted_retry_gates
    from mewcode.kernel.collaboration_faults import ACCEPTANCE_BLOCKERS, report_fault
    recovery_gates = []
    offers = [*execution_recheck_gates(record), *unstarted_retry_gates(record)]
    offered_scopes = {row['scope'] for row in offers}
    gates = [row for row in value.get('recovery_gates', {}).values() if row['scope'] not in offered_scopes]
    for row in [*gates, *offers]:
        if not gate_current(value, row):
            continue
        report = manual_candidate(value, row)
        unknown = bool(unresolved_calls(record, list(value["contracts"]) if row["kind"] == "coordinator_unavailable" else row["node_ids"]))
        recovery_gates.append({**copy.deepcopy(row), "manual_available": bool(row["kind"] != "execution_evidence_available" and report and report.get("artifacts")
            and not unknown and report_fault(report) not in ACCEPTANCE_BLOCKERS), "effects_unresolved": unknown,
            "verifier_switch_available": bool(not unknown and verifier_switch_attempt(value, row)),
            "node_labels": [value["contracts"][key]["label"] for key in row["node_ids"]],
            "checks": copy.deepcopy((report or {}).get("checks", [])), "artifacts": copy.deepcopy((report or {}).get("artifacts", []))})
    editable_entries = [{"id": row["id"], "title": row.get("title", row["id"]), "body": row.get("body", ""),
                         "field": "body", "kind": row["kind"], "editable": row.get("origin") != "host"}
                        for row in value["entries"].values() if row["kind"] in {"goal", "constraint", "acceptance", "decision", "assumption"}]
    editable_entries.extend({"id": key, "title": "指定方法：" + contract["label"], "body": contract["method"]["description"],
        "field": "method", "kind": "node", "editable": value["entries"][key].get("origin") != "host"}
        for key, contract in value["contracts"].items() if contract.get("method") and key in value["entries"])
    editable_entries.extend(row for row in _requirements(value) if row['field'] == 'acceptance')
    return {"spec_version": value["spec_version"], "coordinator_epoch": value["coordinator_epoch"],
            "plan_version": value["plan_version"], "budget": copy.deepcopy(value.get("lineage_budget", {})),
            "milestones": [{"id": row["id"], "goal": row["goal"], "status": row["status"]}
                           for row in value["milestones"].values()],
            "entries": editable_entries,
            "recovery_gates": recovery_gates,
            "pending_revisions": sum(row["status"] in {"proposed", "needs_attention"} for row in value["proposals"].values()),
            "timeline": [{"sequence": row["sequence"], "label": labels.get(row["kind"], "运行记录已更新"),
                          "detail": row.get("reason", "")} for row in value["timeline"][-20:]]}
