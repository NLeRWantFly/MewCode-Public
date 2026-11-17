"""Versioned workflow instructions and bounded, host-derived working context.

The package contains specifications, never another node's execution history.
The summary is navigation over receipts; it cannot grant authority or pass an
acceptance check. Runtime revisions deliberately do not enter package identity.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

SCHEMA_VERSION = 1
MARKER = 'workflow-working-context'
SNAPSHOT_CHARS = 6000
DELTA_CHARS = 1600
MAX_DELTAS = 4
SUBMISSION_TOOLS = frozenset({'SubmitVerdict', 'SubmitHandoff', 'SubmitLoopDecision', 'SubmitAssistanceReceipt'})
STATE_RULE = ('以宿主最新 state 和本轮增量为执行依据；旧对话、旧概要和旧拒收文字只作证据。'
              '已有且版本有效的草稿、回执和证据直接复用；仅当前必需信息缺失或版本变化时，'
              '按具体引用读取缺少的部分，不为恢复进度重读历史。概要不是新授权或验收通过。')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _rows(stage):
    raw = stage.get('tools') or {}
    rows = [dict(row, tool_id=row.get('tool_id') or key) for key, row in raw.items()] if isinstance(raw, dict) else raw
    return sorted((row for row in rows if isinstance(row, dict)
                   and ('epoch' not in stage or row.get('epoch', 0) == stage['epoch'])
                   and (not stage.get('input_revision')
                        or row.get('input_revision', stage['input_revision']) == stage['input_revision'])
                   and (not stage.get('node_attempt') or row.get('node_attempt', stage['node_attempt']) == stage['node_attempt'])),
                  key=lambda row: row.get('declared_at') or row.get('started_at') or row.get('completed_at') or 0)


def _bound_requirements(stage):
    """Use only a host summary bound to this exact stage and input revision."""
    saved = stage.get('workflow_context') or {}
    binding = saved.get('binding') or {}
    keys = ('node_id', 'input_revision', 'node_attempt', 'epoch')
    if (not binding.get('skill_version') or saved.get('skill_version') != binding['skill_version']
            or any(key not in stage or stage[key] != binding.get(key) for key in keys)):
        return None
    return (saved.get('snapshot') or {}).get('requirements')


def _revalidate_submission(row, requirements):
    """Recheck stored arguments against the current tool, never interpret old prose."""
    if (row.get('name') != 'SubmitVerdict' or not row.get('is_error')
            or not isinstance(row.get('args'), dict) or not isinstance(requirements, dict)
            or not isinstance(requirements.get('reviewed_decision_ids'), list)
            or not all(isinstance(key, str) for key in requirements['reviewed_decision_ids'])
            or not isinstance(requirements.get('reviewed_sources'), dict)):
        return None
    from pydantic import ValidationError
    from mewcode.tools.base import parameter_error_result
    from mewcode.tools.submit_verdict import SubmitVerdictParams, SubmitVerdictTool
    try:
        params = SubmitVerdictParams.model_validate(row['args'])
    except ValidationError as error:
        rejected = [parameter_error_result('SubmitVerdict', error)]
    else:
        tool = SubmitVerdictTool()
        tool.required_sources = copy.deepcopy(requirements['reviewed_sources'])
        tool.required_decisions = {key: {} for key in requirements['reviewed_decision_ids']}
        rejected = tool.reference_rejections(params)
    if not rejected:
        return {'status': 'rejected', 'category': 'unclassified', 'waiting_for': 'agent',
                'feedback_basis': 'current_reference_requirements',
                'requirements_revision': _digest(requirements),
                'next_action': '当前 state 的引用要求与已保存草稿一致。复用草稿和有效证据，按当前工具协议提交；'
                               '引用匹配不代表提交已接收或质量验收通过。'}
    feedbacks = [result.host_observation['submission'] for result in rejected]
    feedback = copy.deepcopy(feedbacks[-1])
    for item in feedbacks:
        if 'mismatched_sources' in item:
            feedback['mismatched_sources'] = copy.deepcopy(item['mismatched_sources'])
    feedback['next_action'] = ('当前 state 对已保存草稿的校验结果：'
                               + ''.join(value['next_action'] for value in feedbacks))
    feedback['feedback_basis'] = 'current_reference_requirements'
    feedback['requirements_revision'] = _digest(requirements)
    return feedback


def latest_submission(stage, *, requirements=None):
    """A later read/round limit must not conceal an unresolved submission error."""
    for row in reversed(_rows(stage)):
        if row.get('name') not in SUBMISSION_TOOLS or row.get('status') not in {'completed', 'failed'}:
            continue
        observed = (row.get('host_observation') or {}).get('submission')
        if not isinstance(observed, dict) or (observed.get('category') == 'reference_mismatch'
                and observed.get('code') in {'submission_reference_mismatch', 'submission_source_revision_mismatch'}):
            # Re-evaluate reference errors against current requirements. An old
            # tool message must never become the next action by itself.
            observed = _revalidate_submission(row,
                requirements if requirements is not None else _bound_requirements(stage)) or (
                    observed if isinstance(observed, dict) else {
                        'status': 'rejected' if row.get('is_error') else 'accepted',
                        'category': 'unclassified' if row.get('is_error') else 'submitted',
                        'next_action': ('按当前 state 的任务、工具协议与必需引用复用已有草稿并纠正提交。'
                                        '仅缺少具体字段时定向读取对应内容。' if row.get('is_error') else
                                        '提交已被工具接收；以后续宿主验收和调度状态为准。')})
        compact = {key: copy.deepcopy(observed[key]) for key in (
            'schema_version', 'status', 'category', 'code', 'waiting_for',
            'missing_decision_ids', 'unknown_decision_ids', 'required_decision_ids',
            'mismatched_sources',
            'feedback_basis', 'requirements_revision') if key in observed}
        compact['next_action'] = str(observed.get('next_action') or '')[:1600]
        return {**compact, 'tool': row['name'], 'tool_id': row['tool_id'],
                'draft_ref': {'tool_id': row['tool_id']}}
    return None


def current_handoff(stage):
    """Use the current delivery, including a settled receipt awaiting publication."""
    handoff = stage.get('handoff')
    if handoff is None:
        view = stage.get('ledger_view') or {}
        handoff = (view.get('last_handoff') or {}).get('payload') or {}
    if stage.get('handoff_input_revision', stage.get('input_revision')) != stage.get('input_revision'):
        handoff = {}
    if stage.get('status') != 'completed':
        for row in reversed(_rows(stage)):
            if (row.get('name') == 'SubmitHandoff' and row.get('status') == 'completed'
                    and not row.get('is_error') and isinstance(row.get('args'), dict)):
                return row['args']
    return handoff or {}


def _flow_nodes(flow):
    raw = flow.get('nodes') or []
    return list(raw.values()) if isinstance(raw, dict) else raw


def build_workflow_skill(record, node_id, role, registry):
    """Compile only frozen plan data and this invocation's actual tool schemas."""
    flow = record.get('locked_flow') or {}
    nodes = [row for row in _flow_nodes(flow) if isinstance(row, dict)]
    current = next((row for row in nodes if row.get('node_id', row.get('id')) == node_id), {})
    specification_fields = ('node_id', 'id', 'label', 'role', 'kind', 'responsibility', 'model',
        'members', 'reducer', 'failure_policy', 'pipeline', 'tools', 'deny_tools', 'backup_models',
        'capabilities', 'acceptance_criteria', 'inputs', 'outputs', 'depends_on')
    plan = {'goal': record.get('task') or flow.get('description') or '',
            'nodes': [{key: copy.deepcopy(row[key]) for key in specification_fields if key in row}
                      for row in nodes],
            'edges': copy.deepcopy(flow.get('edges') or []),
            'finalizer_node_id': flow.get('finalizer_node_id'),
            'rules': {key: copy.deepcopy(flow['meta'][key]) for key in (
                'audit_rubrics', 'loops', 'human_gates', 'node_plan_briefs') if key in (flow.get('meta') or {})}}
    stage = {key: copy.deepcopy(current[key]) for key in (
        'node_id', 'id', 'label', 'role', 'kind', 'responsibility', 'acceptance_criteria',
        'inputs', 'outputs', 'depends_on', 'tool_policy') if key in current}
    stage.update(node_id=node_id, role=role)
    contracts = (record.get('collaboration') or {}).get('contracts') or {}
    if node_id in contracts:
        stage['contract'] = {key: copy.deepcopy(contracts[node_id][key]) for key in (
            'id', 'version', 'title', 'label', 'goal', 'objective', 'instructions', 'requirement_ids', 'constraint_ids',
            'acceptance', 'acceptance_criteria', 'checks', 'forbidden_changes', 'method',
            'inputs', 'outputs', 'dependencies') if key in contracts[node_id]}
        keys = set(contracts[node_id].get('requirement_ids', []) + contracts[node_id].get('constraint_ids', []))
        stage['requirements'] = {key: copy.deepcopy(row) for key, row in
            ((record.get('collaboration') or {}).get('entries') or {}).items() if key in keys}
    schemas = {}
    for tool in registry.list_tools():
        if registry.is_enabled(tool.name) and getattr(tool, 'params_model', None) is not None:
            schemas[tool.name] = {'description': tool.description,
                                  'parameters': tool.params_model.model_json_schema()}
    schemas[ReadWorkflowContext.name] = {'description': ReadWorkflowContext.description,
                                        'parameters': ReadWorkflowContextParams.model_json_schema()}
    template = (Path(__file__).parents[1] / 'skills' / 'builtins' / 'workflow-execution' / 'SKILL.md').read_text(encoding='utf-8')
    from mewcode.skills.parser import parse_frontmatter
    _, instructions = parse_frontmatter(template)
    sections = {'plan': plan, 'stage': stage, 'tools': schemas}
    version = _digest({'schema_version': SCHEMA_VERSION, 'instructions': instructions, **sections})
    # The complete map is paged rather than silently dropping later stages.
    route = _json([{key: row[key] for key in ('node_id', 'id', 'label', 'role', 'kind') if key in row}
                   for row in plan['nodes']])
    route = route if len(route) <= 2400 else f"{len(plan['nodes'])} 个阶段；完整地图用 ReadWorkflowContext(section='plan') 分页读取。"
    submissions = sorted(SUBMISSION_TOOLS.intersection(schemas))
    body = (instructions + '\n工作流版本：' + version + '\n当前阶段：' + node_id + '；职责：' + role
            + '\n流程地图：' + route + '\n当前提交工具：' + ', '.join(submissions)
            + '\n当前阶段完整合同见 stage，真实工具定义见 tools；两者均通过 ReadWorkflowContext 按需分页读取。')
    name = 'workflow-' + version[:20]
    markdown = f'---\nname: {name}\ndescription: 本次编排的版本固定流程、阶段职责和实际工具协议。\n---\n\n' + body
    return {'schema_version': SCHEMA_VERSION, 'version': version, 'name': name,
            'body': body, 'markdown': markdown, 'sections': sections}


def save_skill_package(package, work_dir):
    """Write content-addressed specifications, with no runtime history or secrets."""
    root = Path(work_dir) / '.mewcode' / 'workflow-skills' / package['version']
    root.mkdir(parents=True, exist_ok=True)
    (root / 'references').mkdir(exist_ok=True)
    files = {'SKILL.md': package['markdown'], **{
        'references/' + key + '.json': _json(value) for key, value in package['sections'].items()}}
    for name, content in files.items():
        target = root / name
        if not target.exists() or target.read_text(encoding='utf-8') != content:
            target.write_text(content, encoding='utf-8')
    return str(root / 'SKILL.md')


def current_requirements(registry):
    tool = registry.get('SubmitVerdict')
    if tool is None or not registry.is_enabled('SubmitVerdict'):
        return {}
    return {'reviewed_decision_ids': sorted(getattr(tool, 'required_decisions', {})),
            'reviewed_sources': copy.deepcopy(getattr(tool, 'required_sources', {})),
            'reviewed_snapshots': copy.deepcopy(getattr(tool, '_required_reviews', {})),
            'assessment_contract': copy.deepcopy(getattr(tool, '_contract', None))}


def project_working_context(stage, *, binding, requirements=None):
    """No semantic model call, no summary-as-progress, no guessed success."""
    from mewcode.experts.recovery_diagnostics import is_read_receipt, _resource_key
    rows = _rows(stage)
    evidence, writes, submissions = set(), set(), set()
    recent = []
    for row in rows:
        if row.get('status') != 'completed' or row.get('is_error'):
            continue
        if is_read_receipt(row) and row.get('name') != 'ReadWorkflowContext':
            key = _resource_key(row, str(binding.get('input_revision') or ''), str(binding.get('node_id') or ''))
            if key:
                evidence.add(key)
        if row.get('effect_sha256'):
            writes.add(row['effect_sha256'])
        if row.get('name') in SUBMISSION_TOOLS:
            submissions.add(row['tool_id'])
        cursor = (row.get('host_observation') or {}).get('read_cursor')
        if cursor and row.get('name') != 'ReadWorkflowContext':
            recent.append({'tool_id': row['tool_id'], **copy.deepcopy(cursor)})
    feedback = latest_submission(stage, requirements=requirements)
    handoff = current_handoff(stage)
    from mewcode.experts.review_protocol import current_review_protocol, protocol_correction
    review_protocol = current_review_protocol(stage)
    correction = protocol_correction(stage, rows)
    pending = stage.get('pending_permission') or stage.get('pending_interaction') or {}
    waiting = 'user' if (pending.get('question') or pending.get('request_id') or pending.get('tool_id')
                         or any(row.get('status') == 'waiting_permission' for row in rows)) else 'agent'
    if waiting == 'user':
        action = '处理当前宿主保存的待答问题；概要不代替用户答复。'
    elif feedback and feedback.get('status') == 'rejected':
        if feedback.get('waiting_for') == 'host':
            waiting = 'host'
        action = feedback.get('next_action') or '按精确工具回执纠正提交；读取 submission 可恢复完整草稿。'
    elif feedback and feedback.get('status') == 'accepted' and not (
            feedback.get('tool') == 'SubmitHandoff' or stage.get('status') == 'completed'):
        waiting = 'host'
        action = '提交已被工具接收；后续以宿主验收和调度结果为准。'
    elif correction and not (feedback and feedback.get('status') == 'accepted'):
        action = correction
    elif handoff.get('open_items'):
        action = str(handoff['open_items'][0])
    elif feedback and feedback.get('status') == 'accepted':
        waiting = 'host'
        action = '提交已被工具接收；后续以宿主验收和调度结果为准。'
    elif recent and recent[-1].get('more'):
        action = '继续读取所需证据时使用最近回执的 next_offset；无需重读已完成范围。'
    else:
        action = '依据当前合同与已保存证据执行下一项实际工作；未确认的事实保持未确认。'
    value = {'schema_version': SCHEMA_VERSION, 'binding': binding,
             'status': stage.get('status', 'running'), 'waiting_for': waiting,
             'progress': {'evidence_ranges': len(evidence), 'artifact_versions': len(writes),
                          'accepted_submissions': len(submissions),
                          'verified_items': (stage.get('review_progress') or {}).get('complete')},
             'submission': feedback, 'next_action': str(action)[:1600],
             'next_items': ([str(item)[:300] for item in (handoff.get('open_items') or [])[:6]]
                            if waiting == 'agent' and not correction else []),
             'recent_evidence': recent[-4:],
             'requirements': copy.deepcopy(requirements or {}),
             'sources': {'current': "ReadWorkflowContext(section='current')",
                         'submission': "ReadWorkflowContext(section='submission')"}}
    if review_protocol:
        value['review_protocol'] = {key: copy.deepcopy(review_protocol[key]) for key in (
            'input_kind', 'queue_initialized', 'submission_tool')}
        value['review_protocol']['source'] = "ReadStageResult(node_id='@review_protocol')"
    from mewcode.experts.work_reporting import current_report
    reported = current_report(stage)
    if reported:
        # Navigation for this node only; never import another independent
        # reader's observations as evidence or count a report as new work.
        value['reported_work'] = {'progress': copy.deepcopy(reported.get('progress')),
            'issue_count': len(reported.get('issues', [])),
            'issues': [{**{key: row[key] for key in ('id', 'scope', 'location', 'state')},
                        'message': row['message'][:120], 'next_step': row.get('next_step', '')[:120]}
                       for row in sorted(reported.get('issues', []), key=lambda row: row['seq'], reverse=True)[:3]]}
    if len(_json(value)) > SNAPSHOT_CHARS and reported:
        value['reported_work'].pop('issues', None)
    # Mandatory requirements remain intact in their paged source and the
    # frozen task prompt; the navigation card never pretends an excerpt is all.
    if len(_json(value)) > SNAPSHOT_CHARS:
        value['requirements'] = {'details': "ReadWorkflowContext(section='requirements')",
                                 'reviewed_decision_count': len((requirements or {}).get('reviewed_decision_ids', []))}
        value['recent_evidence'] = recent[-1:]
    if len(_json(value)) > SNAPSHOT_CHARS:
        value['submission'] = {key: feedback.get(key) for key in ('tool', 'tool_id', 'status', 'category', 'code')} if feedback else None
        value['recent_evidence'] = []
        value['next_items'] = []
    return value


class WorkingContext:
    """Persist latest snapshot; append bounded deltas without rewriting prefix."""
    def __init__(self, package, binding, *, saved=None):
        self.package, self.binding = package, copy.deepcopy(binding)
        saved = saved if isinstance(saved, dict) and saved.get('binding') == binding else {}
        self.snapshot = copy.deepcopy(saved.get('snapshot') or {})
        self.revision = int(saved.get('revision') or 0)
        self.last_digest = _digest(self.snapshot)
        self.published = None
        self.delta_count = 0
        self.coalesces = 0
        self.requirements = {}
        self.stage = {}

    def observe(self, stage, requirements=None):
        self.stage = stage
        self.requirements = requirements or {}
        snapshot = project_working_context(stage, binding=self.binding, requirements=self.requirements)
        digest = _digest(snapshot)
        if digest != self.last_digest:
            self.revision += 1
            self.snapshot, self.last_digest = snapshot, digest

    def saved(self):
        return {'schema_version': SCHEMA_VERSION, 'binding': self.binding, 'revision': self.revision,
                'snapshot': copy.deepcopy(self.snapshot), 'skill_version': self.package['version'],
                'coalesces': self.coalesces}

    def restore(self, conversation):
        """Pure restoration also runs on temporary compaction budget candidates."""
        conversation.history[:] = [message for message in conversation.history if message.context_kind != MARKER]
        conversation.add_system_reminder(MARKER + '\n' + _json({'revision': self.revision, 'snapshot': self.snapshot})
                                         + '\n' + STATE_RULE)
        conversation.history[-1].transient = True
        conversation.history[-1].context_kind = MARKER

    def sync(self, conversation):
        messages = [message for message in conversation.history if message.context_kind == MARKER]
        if self.published == self.last_digest and messages:
            return
        body = _json({'revision': self.revision, 'snapshot': self.snapshot})
        reset = not messages or self.delta_count >= MAX_DELTAS or self.published is None
        if not reset:
            previous = getattr(self, '_published_snapshot', {})
            delta = {key: val for key, val in self.snapshot.items() if previous.get(key) != val}
            body = _json({'revision': self.revision, 'changes': delta})
            reset = len(body) > DELTA_CHARS
        if reset:
            conversation.history[:] = [message for message in conversation.history if message.context_kind != MARKER]
            if messages:
                conversation.reset_usage_anchor()
                self.coalesces += 1
            self.delta_count = 0
            body = _json({'revision': self.revision, 'snapshot': self.snapshot})
        else:
            self.delta_count += 1
        conversation.add_system_reminder(MARKER + '\n' + body + '\n' + STATE_RULE)
        message = conversation.history[-1]
        message.transient, message.context_kind = True, MARKER
        self.published = self.last_digest
        self._published_snapshot = copy.deepcopy(self.snapshot)


class ReadWorkflowContextParams(BaseModel):
    section: Literal['plan', 'stage', 'tools', 'current', 'requirements', 'submission'] = 'current'
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=1, le=6000)


class ReadWorkflowContext(Tool):
    name = 'ReadWorkflowContext'
    description = ('Read this invocation\'s versioned workflow plan, current stage, actual tool schemas, '
                   'latest host summary, submission requirements or exact last submission draft and feedback. '
                   'Follow returned next_offset for progressive disclosure. Source revisions change with facts; '
                   'restart a page chain if its revision changes. Summaries and drafts grant no authority or acceptance.')
    params_model = ReadWorkflowContextParams
    category = 'read'
    consumes_runtime_slot = False
    is_concurrency_safe = True

    def __init__(self, context):
        self.context = context

    async def execute(self, params):
        ctx = self.context
        if params.section == 'current':
            value = ctx.saved()
        elif params.section == 'requirements':
            value = ctx.requirements
        elif params.section == 'submission':
            feedback = latest_submission(ctx.stage, requirements=ctx.requirements)
            row = next((row for row in _rows(ctx.stage) if feedback and row['tool_id'] == feedback['tool_id']), {})
            value = {'feedback': feedback, 'draft': copy.deepcopy(row.get('args')),
                     'receipt': str(row.get('output') or ''), 'requirements': ctx.requirements}
        else:
            value = ctx.package['sections'][params.section]
        data = _json(value)
        end = min(len(data), params.offset + params.limit)
        revision = _digest(value)
        def render(end):
            return _json({'section': params.section, 'source_revision': revision,
                'offset': params.offset, 'next_offset': end, 'total_chars': len(data), 'content': data[params.offset:end]})
        from mewcode.tools.base import MAX_OUTPUT_CHARS
        while len(render(end)) > MAX_OUTPUT_CHARS:
            end = params.offset + max(1, (end - params.offset) // 2)
        return ToolResult(render(end),
            host_observation={'read_cursor': {'node_id': '@workflow:' + params.section,
                'source_revision': revision, 'offset': params.offset, 'next_offset': end,
                'total_chars': len(data), 'more': end < len(data)}})
