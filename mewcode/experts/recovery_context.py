"""Scoped host facts for recovery; observations never complete business work."""
from __future__ import annotations

import copy
import hashlib
import json
import ntpath
import posixpath
import re
import time

from mewcode.experts.authorization_ledger import covers, entries
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.permissions.reviewer import redact_sensitive

PROBE_TOOLS = ('ReadFile', 'Glob', 'Grep', 'ReadAuditArtifact', 'InspectVisualEvidence', 'ProbeCapabilities',
               'capability_binding', 'VerifyRecoveryCheckpoint')

DIAGNOSTIC_TOOLS = frozenset({'ReadStageResult', 'ReadCaseDossier', 'ReadRecoveryContext',
    'ReadAuthorizations', 'ToolSearch', 'ProbeCapabilities', 'InspectToolHistory'})
READONLY_TOOLS = DIAGNOSTIC_TOOLS | {'ReadFile', 'Glob', 'Grep', 'ReadAuditArtifact', 'InspectVisualEvidence',
    'VerifyRecoveryCheckpoint', 'InspectGitState'}


def tool_effect_pending(tool):
    return (tool.get('status') in {'running', 'waiting_permission'} or
        (tool.get('status') == 'outcome_unknown' and tool.get('name') not in READONLY_TOOLS))


def failed_step(record, node_id):
    stage = record.get('node_states', {}).get(node_id, {})
    epoch = ledger_for_run(record).view(node_id).epoch
    for identity, row in reversed(list((stage.get('tools') or {}).items())):
        if (row.get('epoch', epoch) == epoch and row.get('name') not in DIAGNOSTIC_TOOLS and isinstance(row.get('args'), dict)
                and (row.get('is_error') or row.get('status') == 'failed')):
            return {'tool': row['name'], 'arguments': copy.deepcopy(row['args']), 'tool_id': identity}
    return {}


def probe_restores_step(case, tool, arguments):
    """Reading inventory/history does not prove that the failed operation works."""
    if tool in DIAGNOSTIC_TOOLS or tool in {'Glob', 'Grep', 'capability_binding'}:
        return False
    original = case.get('failed_step') or {}
    if original:
        return tool == original['tool'] and arguments == original['arguments']
    return (case.get('signature', {}).get('capability') in {'vision', 'InspectVisualEvidence'} and
            (tool == 'InspectVisualEvidence' or (tool == 'ReadAuditArtifact' and arguments.get('render_page') is True)))


def original_model(record, node_id):
    value = (record.get('locked_models') or {}).get(node_id)
    if not value:
        value = next((node.get('model') for node in (record.get('locked_flow') or {}).get('nodes', [])
            if node.get('node_id') == node_id), None)
    return copy.deepcopy(value)


def scoped_record(read_record, run_id, node_id, epoch=None, input_revision=None):
    record = copy.deepcopy(read_record() or {})
    validate_scope(record, run_id, node_id, epoch, input_revision)
    return record


def validate_scope(record, run_id, node_id, epoch=None, input_revision=None):
    stage = (record.get('node_states') or {}).get(node_id)
    if not run_id or record.get('run_id') != run_id or not isinstance(stage, dict):
        raise ValueError('恢复证据不属于本次绑定的运行和原节点')
    actual_epoch = ledger_for_run(record).view(node_id).epoch
    if epoch is not None and actual_epoch != epoch:
        raise ValueError('原节点已换代际，旧恢复工具不能核验新检查点')
    if input_revision is not None and stage.get('input_revision', '') != input_revision:
        raise ValueError('原节点输入已变化，请重新读取当前检查点')


def receipt_rows(record, node_id, *, names=None):
    """Only this parent's receipts and its host-owned assistance sessions."""
    stage = record['node_states'][node_id]
    epoch = ledger_for_run(record).view(node_id).epoch
    requests = [row for row in stage.get('assistance_receipts', []) if row.get('epoch', epoch) == epoch]
    current = stage.get('assistance_request') or {}
    request_ids = {row['request_id'] for row in requests if row.get('request_id')}
    if current.get('epoch', epoch) == epoch and current.get('request_id'):
        request_ids.add(current['request_id'])
    owners = {node_id, *[node_id + '__assist_' + identity[:12] for identity in request_ids]}
    rows = []
    for owner in sorted(owners):
        helper = record.get('node_states', {}).get(owner, {})
        for key, tool in (helper.get('tools') or {}).items():
            if names is not None and tool.get('name') not in names:
                continue
            if tool.get('epoch', epoch) != epoch:
                continue
            rows.append({**copy.deepcopy(tool), 'owner_node_id': owner, 'tool_id': key})
    return rows


def context_sections(record, node_id, *, section=None):
    from mewcode.experts.recovery_case import active_case
    stage = record['node_states'][node_id]
    view = ledger_for_run(record).view(node_id)
    case = (active_case(record, node_id) or {}) if section in (None, 'summary') else {}
    observations = []
    commands = []
    names = set()
    if section in (None, 'commands'):
        names.update({'Bash', 'RunVerification'})
    if section in (None, 'summary', 'observations', 'model'):
        names.add('InspectVisualEvidence')
    for row in receipt_rows(record, node_id, names=names) if names else ():
        if row.get('name') in {'Bash', 'RunVerification'}:
            commands.append({key: copy.deepcopy(row.get(key)) for key in
                ('tool_id', 'owner_node_id', 'epoch', 'status', 'is_error', 'args', 'host_observation')})
        if row.get('name') != 'InspectVisualEvidence':
            continue
        host = row.get('host_observation') or {}
        call = host.get('visual_call') or {}
        observations.append({'tool_id': row['tool_id'], 'owner_node_id': row['owner_node_id'],
            'status': row.get('status'), 'is_error': row.get('is_error', False), 'arguments': row.get('args'),
            'pixel_coverage': host.get('pixel_coverage'), 'actual_route': call.get('route'),
            'authorization': call.get('authorization'), 'usage': call.get('usage'),
            'observations': row.get('output'),
            'telemetry_state': 'recorded' if call else 'legacy_missing'})
    grants = entries(record) if section in (None, 'authorizations') else []
    from mewcode.experts.visual_authorization_usage import authorization_accounts
    caps = authorization_accounts(record) if section in (None, 'accounting') else []
    sections = {'summary': lambda: {'run_id': record['run_id'], 'conversation_id': record.get('conversation_id'),
        'node_id': node_id, 'epoch': view.epoch, 'input_revision': stage.get('input_revision', ''),
        'node_status': stage.get('status'), 'case_id': case.get('case_id'), 'case_status': case.get('status'),
        'declared_model': original_model(record, node_id),
        'observation_count': len(observations), 'blocker_count': len(view.active_blockers),
        'supported_probes': list(PROBE_TOOLS), 'business_complete': False},
        'authorizations': lambda: grants, 'observations': lambda: observations, 'commands': lambda: commands,
        'accounting': lambda: {'authorization_limits': caps, 'role_usage': copy.deepcopy(stage.get('usage') or {}),
            'rule': 'role_usage 与视觉授权用量统计范围不同；缺失用量按保守估算单列，不冒充供应商报告计数'},
        'blockers': lambda: [{'fact_id': row.fact_id, **dict(row.payload)} for row in view.active_blockers],
        'tools': lambda: {'original_node': list(stage.get('available_tools') or []),
            'scope': 'saved_original_node_checkpoint', 'live_available': None,
            'note': '这是原节点保存的有效工具清单，不是当前助手工具清单，也不是新实时调用'},
        'model': lambda: {'declared': original_model(record, node_id),
            'observed_routes': [row['actual_route'] for row in observations if row['actual_route']],
            'observed_routes_scope': '仅本节点及其协助会话的 InspectVisualEvidence 宿主工具回执；不覆盖节点自身的模型请求。',
            'transport_evidence': {'request_payload': 'not_exposed_by_this_interface',
                'response_identity': 'not_exposed_by_this_interface',
                'server_effort_attestation': 'not_exposed_by_this_interface'},
            'rule': ('预期绑定、宿主工具实际路线、传输请求与服务端返回分别展示；空 observed_routes '
                     '不表示节点模型未调用、不可用或档位错误。此接口未提供的字段保留未知，'
                     '不反推服务端事实，不为补遥测重复业务调用；按合同检查的适用阶段保留待复核项。')}}
    return {key: factory() for key, factory in sections.items() if section is None or key == section}


def verify_checkpoint(record, node_id, tool_id, *, case_id='', epoch=None, input_revision=None):
    from mewcode.experts.recovery_case import active_case, digest
    view = ledger_for_run(record).view(node_id)
    stage = record['node_states'][node_id]
    case = active_case(record, node_id) or {}
    if (case.get('failed_step') or {}).get('tool') in {'Bash', 'RunVerification'}:
        return _verify_command_checkpoint(record, node_id, tool_id, case=case, view=view,
            case_id=case_id, epoch=epoch, input_revision=input_revision)
    reason, gaps = [], []
    if not case or (case_id and case.get('case_id') != case_id):
        reason.append('没有匹配的当前恢复案件')
    if epoch is not None and epoch != view.epoch:
        reason.append('代际不匹配')
    if input_revision is not None and input_revision != stage.get('input_revision', ''):
        reason.append('输入版本不匹配')
    if case.get('signature', {}).get('capability') not in {'vision', 'InspectVisualEvidence'}:
        reason.append('当前案件不是视觉能力恢复，不能用单页观察解除其他问题')
    if case.get('closed_by_user') or case.get('pending'):
        reason.append('案件仍有用户待决项或已被停止')
    if record.get('status') in {'terminated', 'completed'}:
        reason.append('运行已结束')
    if view.active_blockers or view.pending_question or stage.get('pending_permission') or stage.get('pending_interaction'):
        reason.append('原节点仍有活动阻塞或待答请求')
    row = next((item for item in receipt_rows(record, node_id) if item['tool_id'] == tool_id), None)
    coverage = ((row or {}).get('host_observation') or {}).get('pixel_coverage') or {}
    if not row or row.get('name') != 'InspectVisualEvidence' or row.get('status') != 'completed' or row.get('is_error'):
        reason.append('未找到本节点当前代际成功完成的看图工具回执')
    if coverage.get('complete') is not True or not coverage.get('evidence_ids') or not coverage.get('plan_digest'):
        reason.append('宿主未记录完整像素覆盖')
    model = original_model(record, node_id) or {}
    actual_model = coverage.get('model')
    if not model.get('provider_key') or not model.get('model_id') or actual_model != model.get('model_id'):
        reason.append('观察模型与原节点绑定不匹配或未知')
    owner = (row or {}).get('owner_node_id', node_id)
    helper = record.get('node_states', {}).get(owner, {})
    if owner != node_id:
        handoff = helper.get('handoff') or {}
        if handoff.get('question') or (handoff.get('blockers') and handoff.get('blocker_kind') != 'capability_missing'):
            reason.append('协助会话仍有真实问题或非能力类阻塞')
        if any(receipt.get('authority_needed') or receipt.get('question') for receipt in stage.get('assistance_receipts', [])
            if owner == node_id + '__assist_' + str(receipt.get('request_id', ''))[:12]):
            reason.append('协助回执仍需要具体授权或用户回答')
    if helper.get('pending_permission') or helper.get('pending_interaction') or any(
        tool_effect_pending(tool)
        and not (tool.get('status') == 'running' and tool.get('name') in {'VerifyRecoveryCheckpoint', 'ReadRecoveryContext'})
        for tool in (helper.get('tools') or {}).values()):
        reason.append('工具仍有未决授权或未知执行效果')
    request = {'operation_class': 'external_send', 'scope': {'model_routes': [model.get('provider_key', '') + '/' + model.get('model_id', '')],
        'content_classes': ['page_image'], 'node_ids': [node_id], 'epoch': view.epoch,
        'case_id': case.get('case_id', 'missing'), 'round': len(case.get('rounds', [])) + 1}}
    call = ((row or {}).get('host_observation') or {}).get('visual_call') or {}
    if (call.get('authorization') or {}).get('status') == 'local_route':
        from mewcode.experts.authorization_ledger import Coverage
        check = Coverage('covered')
    else:
        check = covers(record, request)
    if check.status != 'covered':
        reason.append('现有视觉外发授权未覆盖原节点的后续操作，不能凭观察成功扩大授权')
    for key in ('route', 'authorization', 'usage'):
        if not call.get(key):
            gaps.append(key)
    route = call.get('route') or {}
    if route and not route.get('provider_key'):
        gaps.append('provider_key')
    if route and model.get('effort') and not route.get('effort'):
        gaps.append('effort')
    if (route.get('provider_key') and route['provider_key'] != model.get('provider_key')) or (
            route.get('effort') and model.get('effort') and route['effort'] != model['effort']):
        reason.append('已记录的实际路线或档位与原节点绑定不一致')
    return {'schema': 'mewcode.recovery-checkpoint.v1', 'run_id': record['run_id'], 'node_id': node_id,
        'case_id': case.get('case_id'), 'epoch': view.epoch, 'input_revision': stage.get('input_revision', ''),
        'evidence_tool_id': tool_id, 'evidence_digest': digest(row) if row else None,
        'pixel_evidence_ids': coverage.get('evidence_ids', []), 'callback_eligible': not reason,
        'reasons': reason, 'telemetry_gaps': gaps, 'authorization_for_next_action': check.auth_id or None,
        'next_action': 'return_to_original_checkpoint_for_recheck' if not reason else 'preserve_checkpoint',
        'business_complete': False, 'rule': '只核验是否可回原节点继续检查；旧调用凭证缺项不补造，新操作仍须授权与费用检查'}


def _verify_command_checkpoint(record, node_id, tool_id, *, case, view, case_id, epoch, input_revision):
    """Reuse a real, settled native command receipt; never execute a probe."""
    from mewcode.experts.recovery_case import digest
    original = case.get('failed_step') or {}
    stage = record['node_states'][node_id]
    reasons = []
    if not case or (case_id and case['case_id'] != case_id):
        reasons.append('没有匹配的当前恢复案件')
    if epoch is not None and epoch != view.epoch:
        reasons.append('代际不匹配')
    if input_revision is not None and input_revision != stage.get('input_revision', ''):
        reasons.append('输入版本不匹配')
    if case.get('closed_by_user') or case.get('pending') or record.get('status') in {'terminated', 'completed'}:
        reasons.append('案件已停止、运行已结束或仍有用户待决项')
    if view.active_blockers or view.pending_question or stage.get('pending_permission') or stage.get('pending_interaction'):
        reasons.append('原节点仍有活动阻塞或待答请求')
    row = next((item for item in receipt_rows(record, node_id) if item['tool_id'] == tool_id), None)
    host = ((row or {}).get('host_observation') or {}).get('command_execution') or {}
    if not row or row.get('name') != original.get('tool') or row.get('status') != 'completed' or row.get('is_error'):
        reasons.append('未找到原节点当前代际成功完成的同类命令回执')
    try:
        from mewcode.tools.bash import Bash
        from mewcode.tools.run_verification import RunVerification
        model = {'Bash': Bash, 'RunVerification': RunVerification}[original['tool']].params_model
        if model(**((row or {}).get('args') or {})).model_dump() != model(**original['arguments']).model_dump():
            reasons.append('回执对应的命令、超时或发布参数与原失败操作不同')
    except (ValueError, TypeError, KeyError):
        reasons.append('无法核对原操作和命令回执的参数')
    failed_row = next((item for item in receipt_rows(record, node_id)
                       if item['tool_id'] == original.get('tool_id')), {})
    failed_at = failed_row.get('completed_at') or failed_row.get('declared_at')
    if failed_at and ((row or {}).get('declared_at') is None or row['declared_at'] < failed_at):
        reasons.append('成功回执早于本次失败，不能证明当前恢复')
    if (host.get('status') != 'exited' or host.get('command_started') is not True
            or type(host.get('exit_code')) is not int or host['exit_code'] != 0):
        reasons.append('宿主未确认命令实际启动并成功退出')
    if host.get('cleanup_confirmed') is not True or not host.get('transaction_id'):
        reasons.append('缺少已清理的命令级隔离事务回执')
    publication = host.get('publication_state')
    if publication not in {'discarded', 'published', 'no_changes'}:
        reasons.append('命令事务的发布或丢弃状态尚未确认')
    if original.get('arguments', {}).get('publication') == 'discard' and publication != 'discarded':
        reasons.append('原操作要求 discard，回执不能证明已丢弃')
    if host.get('output_truncated') is not False or not isinstance(host.get('stdout'), str):
        reasons.append('命令完整输出缺失或已截断，不能复用为恢复证据')
    owner = (row or {}).get('owner_node_id', node_id)
    helper = record.get('node_states', {}).get(owner, {})
    handoff = helper.get('handoff') or {}
    if owner != node_id and (handoff.get('question') or
            (handoff.get('blockers') and handoff.get('blocker_kind') != 'capability_missing')):
        reasons.append('协助会话仍有真实问题或非能力类阻塞')
    if owner != node_id and helper.get('input_revision', stage.get('input_revision', '')) != stage.get('input_revision', ''):
        reasons.append('协助回执属于另一输入版本')
    if any(receipt.get('authority_needed') or receipt.get('question') for receipt in stage.get('assistance_receipts', [])
           if owner == node_id + '__assist_' + str(receipt.get('request_id', ''))[:12]):
        reasons.append('协助回执仍需要具体授权或用户回答')
    for state in (stage, helper) if owner != node_id else (stage,):
        if state.get('pending_permission') or state.get('pending_interaction') or any(
                tool_effect_pending(tool) and tool.get('name') not in {'VerifyRecoveryCheckpoint', 'ReadRecoveryContext'}
                for tool in (state.get('tools') or {}).values()):
            reasons.append('工具仍有未决授权或未知执行效果')
    return {'schema': 'mewcode.recovery-checkpoint.v1', 'evidence_kind': 'command',
        'run_id': record['run_id'], 'node_id': node_id, 'case_id': case.get('case_id'),
        'epoch': view.epoch, 'input_revision': stage.get('input_revision', ''),
        'evidence_tool_id': tool_id, 'evidence_digest': digest(row) if row else None,
        'command_execution': copy.deepcopy(host), 'callback_eligible': not reasons,
        'reasons': reasons, 'business_complete': False,
        'next_action': 'reuse_command_output_at_original_checkpoint' if not reasons else 'preserve_checkpoint',
        'rule': '只复用同一操作的真实成功回执，不重放命令、不清除授权或业务验收要求'}


def page_context(value, offset, limit):
    def redact(item):
        if isinstance(item, str):
            return redact_sensitive(item)
        if isinstance(item, dict):
            return {key: redact(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [redact(child) for child in item]
        return item
    text = json.dumps(redact(value), ensure_ascii=False, sort_keys=True)
    end = min(len(text), offset + limit)
    revision = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return f'SOURCE_REVISION: {revision}\nTOTAL_CHARS: {len(text)}\nOFFSET: {offset}\nNEXT_OFFSET: {end}\n\n' + text[offset:end]


def reuse_visual_observation(record, node_id, arguments):
    for row in reversed(receipt_rows(record, node_id)):
        if row.get('name') != 'InspectVisualEvidence' or row.get('args') != arguments:
            continue
        if row.get('status') in {'running', 'waiting_permission'} or (
                row.get('status') == 'completed' and not row.get('is_error')):
            # A completed observation with unresolved guards must not trigger a
            # new paid request merely because its callback is still blocked.
            return verify_checkpoint(record, node_id, row['tool_id'])
    return None


# ``Glob`` is intentionally not a generic recovery proof.  A planner may use
# it for the narrow, read-only input-discovery plan below, where the host can
# bind the saved helper receipt to the exact case and query.  Keep this code
# beside the other checkpoint reusers so callers cannot accidentally turn a
# fresh Glob invocation into a recovery callback.
_GLOB_EMPTY = 'No files matched the pattern.'
_GLOB_TRUNCATED = '… (output truncated)'


def _path_style(value):
    value = str(value or '')
    return 'windows' if ntpath.splitdrive(value)[0] or '\\' in value else 'posix'


def _normal_path(value, style=None):
    style = style or _path_style(value)
    return (ntpath.normcase(ntpath.normpath(str(value))) if style == 'windows'
            else posixpath.normpath(str(value)))


def _path_is_under(path, root, style):
    path, root = _normal_path(path, style), _normal_path(root, style)
    if path == root:
        return True
    separator = '\\' if style == 'windows' else '/'
    return path.startswith(root.rstrip('/\\') + separator)


def _input_discovery_plan(case, option=None):
    plan = (case or {}).get('plan') or {}
    options = plan.get('options') or []
    selected_id = plan.get('recommended_option_id')
    selected = option if isinstance(option, dict) else next(
        (item for item in options if item.get('option_id') == selected_id), None)
    if selected is None and len(options) == 1:
        selected = options[0]
    strategy = (selected or {}).get('strategy') or {}
    verification = (selected or {}).get('verification') or {}
    return selected or {}, strategy, verification


def _glob_output_paths(output, arguments):
    """Parse one complete host Glob result without treating prose as paths."""
    text = str(output or '')
    if not text.strip() or text.strip() == _GLOB_EMPTY:
        return None, 'Glob 没有返回任何候选文件'
    if _GLOB_TRUNCATED in text or '... (output truncated)' in text:
        return None, 'Glob 输出已截断，不能作为完整输入清单'
    if text.lstrip().startswith('Error:'):
        return None, 'Glob 返回错误，不能作为输入清单'
    root = str((arguments or {}).get('path') or '')
    pattern = str((arguments or {}).get('pattern') or '')
    if not root or not pattern:
        return None, 'Glob 查询缺少固定 pattern 或 path'
    style = _path_style(root)
    rows, absolute, absolute_keys = [], [], []
    for line in text.splitlines():
        item = line.strip()
        if not item:
            continue
        if item in {_GLOB_EMPTY, _GLOB_TRUNCATED, '... (output truncated)'}:
            return None, 'Glob 输出含非完整文件标记'
        if ntpath.splitdrive(item)[0] or item.startswith(('/', '\\')):
            return None, 'Glob 回执应是相对路径，不能混入绝对路径'
        pieces = item.replace('\\', '/').split('/')
        if any(piece in {'', '.', '..'} for piece in pieces):
            return None, 'Glob 回执包含空路径段或越界路径'
        candidate = (ntpath.join(root, item) if style == 'windows'
                     else posixpath.join(root, item))
        if not _path_is_under(candidate, root, style):
            return None, 'Glob 回执存在查询根目录之外的路径'
        if pattern.lower().endswith('.pdf') and not item.lower().endswith('.pdf'):
            return None, 'Glob 回执包含不符合固定 PDF 查询的路径'
        normalized = _normal_path(candidate, style)
        if normalized in absolute_keys:
            return None, 'Glob 回执包含重复路径'
        rows.append(item)
        absolute.append(ntpath.normpath(candidate) if style == 'windows' else posixpath.normpath(candidate))
        absolute_keys.append(normalized)
    if not rows:
        return None, 'Glob 没有返回完整候选文件'
    return {'relative_paths': rows, 'absolute_paths': absolute, 'root': root,
            'pattern': pattern, 'style': style}, ''


def _handoff_text(helper, receipt):
    handoff = helper.get('handoff') or {}
    parts = [str(receipt.get('notes') or ''), str(handoff.get('summary') or ''),
             str(handoff.get('question_context') or '')]
    for key in ('decisions', 'open_items', 'blockers'):
        parts.extend(str(item) for item in handoff.get(key) or [])
    for item in handoff.get('inline_outputs') or []:
        if isinstance(item, dict):
            parts.extend((str(item.get('name') or ''), str(item.get('content') or '')))
    for item in handoff.get('evidence') or []:
        if isinstance(item, dict):
            parts.extend((str(item.get('ref') or ''), str(item.get('result') or '')))
    return '\n'.join(parts)


def _absolute_paths_in_text(text):
    # File names may contain spaces and CJK characters.  Stop at the suffix,
    # rather than splitting on whitespace, and leave validation to the root
    # check below.
    rows = re.findall(r'(?:(?:[A-Za-z]:[\\/])|/)[^\r\n,;，；*?<>|]+?\.pdf', str(text), flags=re.I)
    return [row.strip().rstrip('。；;,') for row in rows]


def verify_input_discovery(record, node_id, *, case=None, case_id='', option=None):
    """Validate a saved planner input-discovery receipt for host-only reuse.

    This function is deliberately pure.  It does not enumerate the filesystem,
    read a PDF, bind ``run_inputs`` or spend a recovery round.  A successful
    result is only a candidate path fact for the original planner to check.
    """
    from mewcode.experts.recovery_case import active_case, digest

    record = record or {}
    stage = (record.get('node_states') or {}).get(node_id) or {}
    view = ledger_for_run(record).view(node_id)
    current = case or active_case(record, node_id) or {}
    reasons = []
    stored_case = active_case(record, node_id)
    if current.get('node_id') != node_id:
        reasons.append('案件原节点与当前规划节点不一致')
    if not stored_case or stored_case.get('case_id') != current.get('case_id'):
        reasons.append('案件不是当前节点保存的活动案件')
    elif case is not None and digest(stored_case) != digest(current):
        reasons.append('传入案件副本已脱离宿主保存版本')
    selected, strategy, verification = _input_discovery_plan(current, option=option)
    flow_node = next((item for item in (record.get('locked_flow') or {}).get('nodes', [])
                      if isinstance(item, dict) and item.get('node_id') == node_id), {})
    if not current or (case_id and current.get('case_id') != case_id):
        reasons.append('没有匹配的当前恢复案件')
    if current.get('closed_by_user') or current.get('status') not in {'reported', 'awaiting_user', 'executing'}:
        reasons.append('案件已停止或不在可继续状态')
    if record.get('status') in {'terminated', 'completed'}:
        reasons.append('运行已结束')
    if flow_node.get('role') != 'planner':
        reasons.append('输入发现回执只允许回调原规划节点')
    if current.get('epoch') != view.epoch:
        reasons.append('案件代际与当前节点不匹配')
    if current.get('input_revision', '') != stage.get('input_revision', ''):
        reasons.append('案件输入版本与当前节点不匹配')
    # A saved Glob receipt can only repair the input-defect branch that
    # produced it.  Do not let an old round's input explanation override a
    # current authority, business, or unknown-effect decision card.
    plan_root = str(((current.get('plan') or {}).get('root_cause') or {}).get('class') or '')
    report_root = str((current.get('report') or {}).get('root_cause') or '')
    pending = current.get('pending') if isinstance(current.get('pending'), dict) else {}
    if current.get('status') in {'reported', 'awaiting_user'}:
        if report_root != 'input_defect' or (plan_root and plan_root != 'input_defect'):
            reasons.append('当前案件不是输入缺口报告，不能用旧轮次回执解除')
        if pending.get('kind', 'incident') != 'incident':
            reasons.append('当前案件仍有提权、业务或字段待决项')
        if any(pending.get(key) for key in ('authority', 'authority_needed', 'blockers', 'unknown_effect', 'effect_unknown')):
            reasons.append('当前案件仍有授权、业务或未知效果待决项')
    elif current.get('status') == 'executing':
        if plan_root != 'input_defect':
            reasons.append('当前执行轮次不是输入缺口方案')
        if pending:
            reasons.append('当前执行案件仍有用户待决项')
    selected_tools = set(strategy.get('tools') or [])
    if strategy.get('kind') != 'input_discovery' or selected_tools != {'Glob', 'ReadStageResult'}:
        reasons.append('当前方案不是仅含 Glob 与 ReadStageResult 的 input_discovery 方案')
    if verification.get('tool') != 'Glob':
        reasons.append('输入发现方案没有绑定 Glob 核验')
    arguments = copy.deepcopy(verification.get('arguments') or {})
    parsed, parse_error = _glob_output_paths('', arguments)
    if not arguments.get('pattern') or not arguments.get('path'):
        reasons.append(parse_error or '输入发现查询参数不完整')
    receipt_ref = ''
    rounds = current.get('rounds') or []
    if current.get('status') == 'executing':
        # An interrupted execution always binds to its *current* round.  Do
        # not fall back to the last finished round: its helper may have a
        # valid Glob result but belongs to a previous request/plan.
        expected_round = len(rounds) + 1
        raw_round = (current.get('plan') or {}).get('round')
        try:
            round_number = expected_round if raw_round in (None, '') else int(raw_round)
        except (TypeError, ValueError):
            round_number = -1
        if round_number != expected_round:
            reasons.append('当前执行方案轮次与案件下一轮不一致')
        receipt_ref = digest([current.get('case_id'), round_number])[:32] if round_number > 0 else ''
    else:
        last_round = rounds[-1] if rounds else {}
        if last_round:
            if last_round.get('root_cause') != 'input_defect' or last_round.get('verification', {}).get('result') not in {'unchanged', 'changed'}:
                reasons.append('没有可复用的输入缺口核验轮次')
            receipt_ref = str(last_round.get('executor_receipt_ref') or '')
        else:
            reasons.append('没有可复用的输入缺口核验轮次')
            receipt_ref = ''
    receipt = next((row for row in reversed(stage.get('assistance_receipts') or [])
                    if row.get('request_id') == receipt_ref and row.get('node_id') == node_id
                    and row.get('epoch', view.epoch) == view.epoch), None)
    if not receipt_ref or receipt is None:
        reasons.append('没有本节点当前代际已接受的协助回执')
    if receipt and (receipt.get('next_step') != 'resume' or receipt.get('question')
                    or receipt.get('authority_needed')):
        reasons.append('协助回执仍有问题、授权或用户待决项')
    helper_id = node_id + '__assist_' + receipt_ref[:12] if receipt_ref else ''
    helper = (record.get('node_states') or {}).get(helper_id) or {}
    if not helper_id or not helper:
        reasons.append('缺少绑定的宿主协助会话')
    if helper and helper.get('input_revision', stage.get('input_revision', '')) != stage.get('input_revision', ''):
        reasons.append('协助会话属于另一输入版本')
    handoff = helper.get('handoff') or {}
    if helper and helper.get('status') != 'completed':
        reasons.append('协助会话未实际完成')
    if not handoff or handoff.get('status') != 'completed':
        reasons.append('协助会话没有提交已完成的 SubmitHandoff')
    if handoff.get('question') or handoff.get('blockers') or handoff.get('authority_needed'):
        reasons.append('协助交接仍有问题、阻塞或授权请求')
    if stage.get('pending_permission') or stage.get('pending_interaction') or helper.get('pending_permission') or helper.get('pending_interaction'):
        reasons.append('节点或协助会话仍有待授权或待回答项')
    if view.active_blockers:
        reasons.append('原节点仍有活动阻塞')
    if view.pending_question:
        reasons.append('原节点仍有待回答问题')
    from mewcode.experts.recovery_context import tool_effect_pending as _pending
    if any(_pending(tool) for state in (stage, helper)
           for tool in (state.get('tools') or {}).values()):
        reasons.append('节点或协助会话仍有未知工具效果')
    glob_rows = [
        {**copy.deepcopy(tool), 'tool_id': tool_id}
        for tool_id, tool in (helper.get('tools') or {}).items()
        if tool.get('name') == 'Glob' and tool.get('epoch', view.epoch) == view.epoch
    ]
    glob = next((tool for tool in reversed(glob_rows)
                 if tool.get('args') == arguments), None)
    if glob is None:
        reasons.append('没有匹配固定查询参数的宿主 Glob 回执')
    elif glob.get('status') != 'completed' or glob.get('is_error'):
        reasons.append('Glob 回执未成功完成')
    parsed = None
    parse_error = ''
    if glob is not None and glob.get('status') == 'completed' and not glob.get('is_error'):
        parsed, parse_error = _glob_output_paths(glob.get('output'), arguments)
        if parse_error:
            reasons.append(parse_error)
    absolute_paths = (parsed or {}).get('absolute_paths', [])
    if parsed and helper:
        text = _handoff_text(helper, receipt or {})
        style = parsed['style']
        expected_text = {_normal_path(path, style) for path in absolute_paths}
        discovered = {_normal_path(path, style) for path in _absolute_paths_in_text(text)}
        if not expected_text.issubset(discovered):
            reasons.append('SubmitHandoff 没有提交完整绝对路径清单')
        if any(not _path_is_under(path, parsed['root'], style) for path in discovered):
            reasons.append('SubmitHandoff 含有查询根目录之外的路径')
        if any(path not in expected_text for path in discovered):
            reasons.append('SubmitHandoff 含有 Glob 未返回的额外路径')
    callback = not reasons
    evidence_digest = digest({'glob': glob, 'handoff': handoff, 'receipt': receipt,
                              'case_id': current.get('case_id'), 'epoch': view.epoch,
                              'input_revision': stage.get('input_revision', '')})
    return {'schema': 'mewcode.input-discovery-recovery.v1', 'run_id': record.get('run_id'),
        'node_id': node_id, 'case_id': current.get('case_id'), 'epoch': view.epoch,
        'input_revision': stage.get('input_revision', ''), 'callback_eligible': callback,
        'reasons': reasons, 'business_complete': False, 'input_bound': False,
        'run_inputs_unchanged': True, 'evidence_digest': evidence_digest,
        'evidence_tool_id': glob.get('tool_id') if glob else None,
        'helper_node_id': helper_id or None, 'executor_receipt_ref': receipt_ref or None,
        'relative_paths': (parsed or {}).get('relative_paths', []),
        'absolute_paths': absolute_paths, 'query': arguments,
        'sources': {'helper_node_id': helper_id or None,
            'glob_tool_id': glob.get('tool_id') if glob else None,
            'executor_receipt_ref': receipt_ref or None,
            'handoff_status': handoff.get('status') if handoff else None},
        'case_digest': digest(current),
        'next_action': 'return_to_original_planner_for_input_check' if callback else 'preserve_checkpoint',
        'rule': '只复用当前规划节点已接受的只读 Glob 与 SubmitHandoff 路径事实；不绑定运行输入、不代替原节点交接或业务验收'}


def reuse_input_discovery(record, node_id, *, case=None, case_id='', option=None):
    checked = verify_input_discovery(record, node_id, case=case, case_id=case_id, option=option)
    return checked if checked.get('callback_eligible') else None


def input_discovery_resume_message(checked, *, no_new_round=False):
    source = {
        'helper_node_id': checked.get('helper_node_id'),
        'glob_tool_id': checked.get('evidence_tool_id'),
        'executor_receipt_ref': checked.get('executor_receipt_ref'),
        'query': checked.get('query'),
    }
    prefix = ('宿主已复用当前规划节点已接受的只读输入发现回执，未重新调用模型、工具或增加恢复轮次。'
              if no_new_round else
              '本轮结构化恢复已复用当前规划节点已接受的只读输入发现回执，未重新枚举资料。')
    return (prefix + '以下是候选事实；原节点仍须自行核对输入并提交 SubmitHandoff，'
        '不能据此标记业务完成或绑定未声明运行参数。\n' +
        json.dumps({'paths': checked.get('absolute_paths') or [],
                    'sources': source,
                    'relative_paths': checked.get('relative_paths') or [],
                    'evidence_digest': checked.get('evidence_digest')}, ensure_ascii=False))


def reconcile_input_discovery_record(record, case_id, *, expected_case_digest='', no_new_round=True):
    """Atomically prepare a host-only saved input-discovery callback.

    The caller supplies a mutable run snapshot (usually a store transaction).
    This helper performs the final source recheck and only then records the
    independent reconciliation fact.  It never changes run inputs or rounds.
    """
    from mewcode.experts.recovery_case import apply_event, digest, get_case
    case = get_case(record, case_id)
    if case is None or (expected_case_digest and digest(case) != expected_case_digest):
        return None
    checked = verify_input_discovery(record, case['node_id'], case=case, case_id=case_id)
    if not checked.get('callback_eligible'):
        return None
    identity = 'reconcile:input_discovery:' + checked['evidence_digest']
    apply_event(record, case_id, {'kind': 'reconcile', 'identity': identity,
        'case_digest': digest(case), 'evidence': checked, 'recorded_at': time.time()})
    stage = record['node_states'][case['node_id']]
    stage.update(status='running', assistance_resume_message=input_discovery_resume_message(
        checked, no_new_round=no_new_round))
    stage.pop('pending_recovery_case', None)
    ledger_for_run(record).set_phase(case['node_id'], 'running', reason='input_discovery_reconciled')
    return {'case': get_case(record, case_id), 'evidence': checked}
