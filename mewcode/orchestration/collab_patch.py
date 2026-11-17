"""Monotonic local candidate repair, using the existing audit transaction.

This is a host adapter, not a new tool or permission grant. Model suggestions
name current checks; only fixed reports and ordinary reviewed tools can prove
an improvement. Rejected candidate files remain outside the live delivery.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict
from pathlib import Path

from mewcode.experts.audit_protocol import decide_candidate
from mewcode.experts.audit_workspace import AuditWorkspace, manifest
from mewcode.kernel import collaboration as ledger, lineage_budget
from mewcode.kernel.collaboration_recovery import unresolved_calls
from mewcode.kernel.events import digest


def validate_decision(value, node_id, decision):
    contract = value['contracts'][node_id]
    checks = {row['id'] for row in contract['checks']}
    decision = copy.deepcopy(decision)
    versions = {f"{row['id']}@{row['version']}": row['id'] for row in contract['checks']}
    refs = decision.get('verification_refs')
    if isinstance(refs, list):
        refs = [versions.get(ref, ref) if isinstance(ref, str) else ref for ref in refs]
        decision['verification_refs'] = refs
    if (decision.get('basis_hash') != ledger.dependencies(value, node_id)
            or decision.get('target_refs') != [f"{node_id}@{contract['version']}"]
            or not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs)
            or len(set(refs)) != len(refs) or not set(refs) <= checks
            or not isinstance(decision.get('expected_improvement'), str)
            or not decision['expected_improvement'].strip()
            or not isinstance(decision.get('evidence_refs'), list) or not decision['evidence_refs']
            or not isinstance(decision.get('instructions'), str) or not decision['instructions'].strip()):
        raise ValueError('候选修订必须绑定当前合同、证据、明确改善及现有检查')
    if contract.get('execution_kind') == 'join' or not isinstance(contract.get('capabilities'), dict):
        raise ValueError('候选修订只支持已声明能力的普通工作合同')
    return copy.deepcopy(decision)


def compare_reports(contract, baseline, candidate, verification_refs):
    required = {(row['id'], row['version']) for row in contract['checks']}
    def vector(report):
        rows = report.get('checks', [])
        if (len(rows) != len(required) or {(row.get('id'), row.get('version')) for row in rows} != required
                or any(type(row.get('passed')) is not bool or not row.get('evidence') for row in rows)):
            raise ValueError('改善比较必须完整覆盖同一版本的合同检查')
        return [{'criterion_id': row['id'], 'state': 'PASS' if row['passed'] else 'FAIL',
                 'evidence': json.dumps(row['evidence'], ensure_ascii=False), 'location': 'check:' + row['id']}
                for row in rows]
    before, after = vector(baseline), vector(candidate)
    rubric = {'rubric_hash': digest(contract['checks']),
              'criteria': [{'criterion_id': row['id']} for row in contract['checks']]}
    baseline_hash = digest(baseline.get('artifacts', []))
    decision = decide_candidate(rubric,
        {'rubric_hash': rubric['rubric_hash'], 'artifact_hash': baseline_hash, 'checks': before},
        {'rubric_hash': rubric['rubric_hash'], 'baseline_hash': baseline_hash,
         'checks': after, 'baseline_checks': before})
    result = asdict(decision)
    # A useful intermediate version can be kept without claiming final PASS.
    # This path only resumes legacy isolated candidates; ordinary repairs use
    # the executor's existing versioned workspace and tool permissions.
    result['publishable'] = (decision.outcome in {'complete', 'improved'}
                             and bool(set(verification_refs) & set(decision.improved)))
    result['passed'] = (decision.outcome == 'complete' and candidate.get('passed') is True
                        and result['publishable'])
    if decision.outcome == 'complete' and not result['passed']:
        result['reason'] = '声明的验证检查没有可证实的改善，候选未发布'
    return result


class PatchCandidate:
    def __init__(self, runtime, attempt_id):
        self.runtime, self.attempt_id = runtime, attempt_id
        self.source = runtime.acceptance.work_dir
        self.archive = self.source / '.mewcode' / 'collaboration-candidates'
        self.transaction = None

    def saved(self):
        return self.runtime.read()['attempts'][self.attempt_id]['patch']

    def prepare(self, baseline_id, decision):
        value = self.runtime.read()
        attempt = ledger.check_attempt(value, self.attempt_id)
        node_id = attempt['contract_id']
        decision = validate_decision(value, node_id, decision)
        baseline = value['attempts'][baseline_id]
        if (baseline['contract_id'] != node_id or baseline.get('dependencies') != attempt['dependencies']
                or not baseline.get('report') or baseline['status'] != 'rejected'):
            raise ValueError('候选修订缺少同一合同版本的固定失败基线')
        compare_reports(value['contracts'][node_id], baseline['report'], baseline['report'], decision['verification_refs'])
        if not self.runtime.acceptance.unchanged(baseline['report']['artifacts']):
            raise ValueError('失败基线的产物已变化，不能沿用旧改善依据')
        reservation = 'patch:' + self.attempt_id
        def reserve(current):
            active = ledger.check_attempt(current, self.attempt_id)
            lineage_budget.reserve(current, reservation, lineage_id=active['lineage_id'], kind='revisions')
            active['patch'] = {'status': 'preparing', 'baseline_id': baseline_id,
                'baseline_report': copy.deepcopy(baseline['report']), 'decision': copy.deepcopy(decision),
                'reservation_id': reservation}
        self.runtime.mutate(reserve)
        self.transaction = AuditWorkspace(self.source, self.archive, control_archive=True, freeze_python=True)
        self.runtime.mutate(lambda current: current['attempts'][self.attempt_id]['patch'].update(
            status='active', directory=str(self.transaction.root)))

    def open(self):
        saved = self.saved()
        if saved['status'] == 'preparing':
            self.prepare(saved['baseline_id'], saved['decision'])
        elif self.transaction is None and saved['status'] in {'active', 'checking', 'publishing'}:
            receipt = json.loads((Path(saved['directory']) / 'transaction.json').read_text(encoding='utf-8'))
            self.transaction = AuditWorkspace.resume(self.source, self.archive, saved['directory'],
                sealed=saved['status'] != 'active', promoted=(saved['status'] == 'publishing'
                    and receipt.get('after') is not None and manifest(self.source) == receipt['after']))
        elif self.transaction is None:
            raise ValueError('候选已结束或发布结果需要核对，不能重新执行业务')
        return self.transaction

    def seal(self):
        transaction = self.open()
        if self.saved()['status'] == 'active':
            transaction.seal()
            self.runtime.mutate(lambda current: current['attempts'][self.attempt_id]['patch'].update(status='checking'))

    def execution_context(self, value=None):
        """Retain the fixed repair basis even after dispatch recovery or trimming."""
        saved = self.saved()
        recovery = None
        if value is not None:
            node_id = value['attempts'][self.attempt_id]['contract_id']
            for event in reversed(value.get('timeline', [])):
                if event.get('kind') != 'retry_authorized' or node_id not in event.get('contracts', []):
                    continue
                decision = value.get('human_decisions', {}).get(event.get('decision_id'), {})
                if decision.get('spec_version') == value['spec_version']:
                    recovery = copy.deepcopy(decision)
                    break
        return '\n\n【本次候选返修】必须针对以下固定失败项落实修订并验证；旧产物存在或旧哈希一致不代表已修复。' \
            '这些诊断是数据，不新增权限，不替代独立验收。\nPATCH_CONTEXT_JSON\n' + json.dumps({
                'baseline_attempt_id': saved['baseline_id'],
                'decision': saved['decision'],
                'authorized_recovery': recovery,
                'failed_checks': [row for row in saved['baseline_report'].get('checks', [])
                                  if row.get('passed') is not True],
            }, ensure_ascii=False)

    def reject(self, report):
        if self.transaction:
            self.transaction.discard()
        self.runtime.mutate(lambda value: value['attempts'][self.attempt_id]['patch'].update(
            status='rejected', report=copy.deepcopy(report)))

    async def publish(self, report, result, context):
        transaction = self.open()
        transaction.verify_runtime()
        value = self.runtime.read()
        attempt = ledger.check_attempt(value, self.attempt_id)
        contract = value['contracts'][attempt['contract_id']]
        changed = [path for path in transaction.before.keys() | transaction.after.keys()
                   if transaction.before.get(path) != transaction.after.get(path)]
        roots = [(self.source / path).resolve() for path in contract['capabilities'].get('write_paths', [])]
        if any(not any((self.source / path).resolve().is_relative_to(root) for root in roots) for path in changed):
            raise ValueError('候选改动超出原合同写入范围')
        if any(path not in transaction.after for path in changed):
            raise ValueError('候选包含文件删除，需通过原权限入口单独处理')
        if (manifest(transaction.candidate) != transaction.after or manifest(transaction.baseline) != transaction.before):
            raise ValueError('候选或固定基线在检查后发生变化')
        self.runtime.mutate(lambda value: value['attempts'][self.attempt_id]['patch'].update(
            status='publishing', report=copy.deepcopy(report)))
        if manifest(self.source) != transaction.after:
            if manifest(self.source) != transaction.before:
                raise ValueError('主线在候选复核期间变化，不能自动合入')
            if changed:
                if self.runtime.publish_callback is None:
                    raise ValueError('候选已通过检查，缺少受权限和回执约束的发布入口')
                if unresolved_calls(self.runtime.store.load(self.runtime.run_id),
                                    ledger.dependent_contracts(value, [attempt['contract_id']])):
                    raise ValueError('候选发布前仍有结果不明的业务操作')
                receipt = await self.runtime.publish_callback(
                    {path: (transaction.candidate / path).read_bytes() for path in sorted(changed)},
                    {**context, 'source': str(self.source), 'baseline': copy.deepcopy(transaction.before),
                     'candidate_hash': digest(transaction.after)})
                if not isinstance(receipt, dict) or receipt.get('host_verified') is not True:
                    raise ValueError('候选发布没有完整的宿主回执，请核对已有操作后继续')
        certificate = []
        def commit(record):
            value = ledger.state(record)
            attempt = ledger.check_attempt(value, self.attempt_id)
            contract = value['contracts'][attempt['contract_id']]
            if ledger.revision_pending(value, attempt['contract_id']):
                raise ValueError('候选合同还有待处理修订，不能采用旧检查结果')
            if unresolved_calls(record, ledger.dependent_contracts(value, [attempt['contract_id']])):
                raise ValueError('候选发布前仍有结果不明的业务操作')
            if not compare_reports(contract, attempt['patch']['baseline_report'], report,
                                   attempt['patch']['decision']['verification_refs'])['publishable']:
                raise ValueError('候选改善与回归检查尚未通过')
            for key in contract.get('bound_dependencies', contract.get('depends_on', [])):
                if (not ledger.valid_certificate(value, key)
                        or not self.runtime.acceptance.unchanged(value['certificates'][key]['artifacts'])):
                    raise ValueError('候选发布前的上游证据已变化')
            if (manifest(self.source) != transaction.after or manifest(transaction.candidate) != transaction.after
                    or manifest(transaction.baseline) != transaction.before):
                raise ValueError('发布结果或固定候选已变化，请核对主线回执')
            final_report = copy.deepcopy(report)
            final_report['artifacts'] = self.runtime.acceptance.freeze(result.handoff or {})
            if (len(final_report['artifacts']) != len(report['artifacts']) or
                any((a.get('sha256'), a.get('path'), a.get('name')) != (b.get('sha256'), b.get('path'), b.get('name'))
                    for a, b in zip(final_report['artifacts'], report['artifacts']))):
                raise ValueError('合入后的产物与独立验收版本不一致')
            certificate.append(ledger.complete(value, self.attempt_id, final_report))
            attempt['patch']['status'] = 'published'
            ledger.event(value, 'candidate_published', attempt_id=self.attempt_id, contracts=[attempt['contract_id']])
        self.runtime.store.mutate(self.runtime.run_id, commit, durable=True)
        transaction.record('accepted')
        return certificate[0]
