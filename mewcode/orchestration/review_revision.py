"""Host-bound revision evidence for focused repairs and independent reviews.

This is context, never an acceptance shortcut. Current checks, artifacts and
execution evidence still reach the reviewer in full.
"""
from __future__ import annotations

import copy
import difflib
import hashlib

from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import review_binding
from mewcode.kernel.events import digest


REVISION_REVIEW_RULES = (
    '有 revision_context 时，先核对上一轮全部问题、本轮产物差异及其影响，再提交完整验收结论。'
    'sources_unchanged=true 只允许引用原复核中对未受影响固定来源的具体证据，'
    '不是继承 PASS；新交付的事实主张、执行行为、修改和回归仍须独立核验。'
    'sources_unchanged=false 时历史证据只作线索，须按当前来源重新核对。'
    '差异标为 omitted 时读取完整固定版本，不能当作无变化；新增、删除和改名均须检查。'
    '一次列全当前能证实的问题，修复同一问题沿用原 issue_id；每项说明违反哪条现有标准、'
    '具体修正及验证方式，避免把纯风格偏好升级为阻断。'
    '只读取证合同准确列清已查范围、可证事实和未知项即可按该合同判断；'
    '未知的业务证据仍交原审计处置，不能要求调查报告凭文字消除它，也不能据此放行业务。'
)


def latest_report(value, node_id, *, before=None):
    rows = [row for row in ledger.attempts_for(value, node_id)
            if row.get('spec_version') == value.get('spec_version')
            and not row.get('superseded') and row.get('report')
            and row.get('status') in {'accepted', 'rejected'}
            and (before is None or row['number'] < before)]
    return rows[-1] if rows else None


def refresh_previous_delivery(value, node_id, sources):
    """Immediate retries must read the delivery their latest feedback describes."""
    sources = dict(sources or {})
    key = 'previous:' + node_id
    sources.pop(key, None)
    previous = latest_report(value, node_id)
    if previous:
        source = {'node_id': key, 'attempt_id': previous['id'], 'status': previous['status'],
            'handoff': copy.deepcopy(previous.get('delivery', {}).get('handoff') or {}),
            'report': copy.deepcopy(previous['report']),
            'same_dependencies': previous.get('dependencies') == ledger.dependencies(value, node_id),
            'instruction': '本节点最新已复核交付与完整修订项。逐项落实 acceptance_test，保留未受影响内容；'
                '只修结论时直接修订内联正文，无须重读未变化来源。来源或要求变化时重新核验。'
                '完整交付仍须 SubmitHandoff；历史内容不构成新授权或本轮通过。'}
        source['source_revision'] = digest(source)
        sources[key] = source
    return sources


def evidence_basis(record, context):
    sources = getattr(context.get('policy'), 'stage_inputs', None) or {}
    return digest({'sources': {key: row for key, row in sources.items() if not key.startswith('previous:')},
        'authorizations': record.get('authorizations'),
        'authorization_manifest': record.get('authorization_manifest'),
        'authorized_revision': record.get('authorized_revision')})


def repair_stall_basis(record, node_id, policy):
    value = ledger.state(record)
    return digest({'dependencies': ledger.dependencies(value, node_id),
        'epoch': value['coordinator_epoch'],
        'generation': value.get('retry_generations', {}).get(node_id, 0),
        'instructions': value.get('rework_instructions', {}).get(node_id),
        'evidence': evidence_basis(record, {'policy': policy}),
        'model': (record.get('locked_models') or {}).get(node_id),
        'roles': record.get('role_models')})


def _artifact_index(artifacts):
    result = {}
    for row in artifacts:
        kind = row.get('kind')
        name = row.get('path') if kind == 'file' else row.get('name')
        if kind not in {'inline', 'file'} or not isinstance(name, str) or not row.get('sha256'):
            return None
        key = (kind, name)
        if key in result:
            return None
        if kind == 'inline' and (not isinstance(row.get('content'), str)
                or hashlib.sha256(row['content'].encode('utf-8')).hexdigest() != row['sha256']):
            return None
        result[key] = row
    return result


def artifact_changes(before, after):
    old, new = _artifact_index(before), _artifact_index(after)
    if old is None or new is None:
        return None
    changes, remaining = [], 12000
    for kind, name in sorted(old.keys() | new.keys()):
        left, right = old.get((kind, name)), new.get((kind, name))
        status = ('added' if left is None else 'removed' if right is None else
                  'unchanged' if left['sha256'] == right['sha256'] else 'changed')
        row = {'kind': kind, 'name': name, 'status': status,
               'before_sha256': left['sha256'] if left else None,
               'after_sha256': right['sha256'] if right else None}
        if kind == 'file':
            row.update(before_snapshot=left.get('snapshot_path') if left else None,
                       after_snapshot=right.get('snapshot_path') if right else None)
        elif status == 'changed':
            # Only optional diffs are bounded. Never truncate the actual new
            # delivery, old findings or the current acceptance requirements.
            if len(left['content']) + len(right['content']) <= 40000:
                patch = ''.join(difflib.unified_diff(left['content'].splitlines(keepends=True),
                    right['content'].splitlines(keepends=True), fromfile='previous/' + name,
                    tofile='current/' + name, n=3))
                if len(patch) <= min(remaining, 8000):
                    row['inline_diff'] = patch
                    remaining -= len(patch)
                else:
                    row['diff_omitted'] = '读取完整固定交付；差异超过展示预算。'
            else:
                row['diff_omitted'] = '读取完整固定交付；差异超过展示预算。'
        changes.append(row)
    return changes


def review_revision(value, payload, context, reviewer, current_evidence_basis):
    attempt = value['attempts'].get(context['attempt_id'])
    if not attempt:
        return None  # Whole-milestone/final checks keep their existing scope.
    previous = latest_report(value, context['node_id'], before=attempt['number'])
    if (not previous or previous.get('dependencies') != attempt.get('dependencies')
            or previous.get('dependencies') != ledger.dependencies(value, context['node_id'])):
        return None
    report = previous['report']
    if report.get('execution_error') or report.get('artifacts_unchanged') is not True:
        return None
    check = payload['check']
    prior = next((row for row in report.get('checks', [])
                  if row['id'] == check['id'] and row['version'] == check['version']), None)
    if not prior or prior.get('deferred') or not prior.get('evidence') or prior['evidence'].get('execution_error'):
        return None
    # A report alone is not independent evidence. Require its finished host
    # call, exact check/handoff binding and the same selected reviewer.
    matches = []
    for key, call in value['calls'].items():
        binding = call.get('binding') or {}
        round_number = binding.get('acceptance_round')
        if (call.get('kind') != 'acceptance' or call.get('status') != 'finished'
                or call.get('model') != reviewer or call.get('result') != prior['evidence']
                or type(round_number) is not int or round_number < 0):
            continue
        expected_id = digest(['acceptance', previous['id'], round_number, check, report.get('artifacts', [])])
        expected_binding = {**review_binding({'node_id': context['node_id'], 'attempt_id': previous['id'],
            'acceptance_round': round_number}, check), 'call_id': expected_id,
            'handoff_hash': digest(previous.get('delivery', {}).get('handoff') or {})}
        if key == expected_id and binding == expected_binding:
            matches.append((key, call))
    if not matches:
        return None
    call_id, call = matches[-1]
    changes = artifact_changes(report.get('artifacts', []), payload['artifacts'])
    if changes is None:
        return None
    evidence = prior['evidence']
    return {'baseline_attempt_id': previous['id'], 'baseline_call_id': call_id,
        'baseline_report_hash': digest(report), 'basis': attempt['dependencies'],
        'sources_unchanged': call.get('review_evidence_basis') == current_evidence_basis,
        'previous_source': 'previous:' + context['node_id'],
        'previous_check': {'id': prior['id'], 'version': prior['version'], 'passed': prior['passed'],
                           'issues': copy.deepcopy(evidence.get('issues') or []),
                           'evidence': copy.deepcopy(evidence.get('evidence') or [])},
        'artifact_changes': changes,
        'instruction': '差异与历史证据用于复核定位，不继承通过；当前执行与所有变更仍须独立核验。'}


def compact_revision(revision):
    """Drop only optional inline diffs, making every omission explicit."""
    revision = copy.deepcopy(revision)
    for row in revision['artifact_changes']:
        if row.pop('inline_diff', None) is not None:
            row['diff_omitted'] = '读取完整固定交付；上下文预算未包含差异正文。'
    return revision
