"""Durable goal-directed checks at recovery safe points, never a retry quota.

Only host observations are accepted here. Model narration, changed error text,
new log files and a successful capabilities query are not restoration evidence.
"""
from __future__ import annotations

import copy
import math

from mewcode.experts.recovery_case import digest


class RecoveryBoundary:
    def __init__(self, state=None):
        self.state = copy.deepcopy(state or {})
        for key, default in {'checks': [], 'consumed_evidence': [], 'stagnation': 0,
                             'active_seconds': 0., 'needs_review': False}.items():
            self.state.setdefault(key, default)

    def set_goal(self, goal, *, target):
        # The original failed operation remains the restoration criterion.
        self.state['original_target'] = target
        if isinstance(goal, dict) and goal.get('target'):
            self.state['goal'] = copy.deepcopy(goal)
        self.state.setdefault('goal', {'target': target, 'milestones': [],
                                      'stop_when': ['原失败步骤恢复', '需要新增授权或业务决定']})

    def check(self, *, identity, method, seconds, evidence_refs=(), restored=False,
              hard_reason='', expected_seconds=None):
        previous = next((c for c in self.state['checks'] if c['identity'] == identity), None)
        if previous:
            return previous
        seconds = max(0., float(seconds)) if math.isfinite(float(seconds)) else 0.
        self.state['active_seconds'] += seconds
        seen = set(self.state['consumed_evidence'])
        evidence = sorted(set(filter(None, evidence_refs)) - seen)
        progress = bool(evidence) or restored
        self.state['stagnation'] = 0 if progress else self.state['stagnation'] + 1
        recent = self.state['checks'][-1:]
        repeated = bool(recent and recent[0]['method'] == method and not progress)
        trigger = 'milestone' if progress else 'stagnation'
        if (not progress and isinstance(expected_seconds, (int, float))
                and math.isfinite(expected_seconds) and expected_seconds > 0
                and seconds >= expected_seconds * 1.5):
            trigger = 'overdue'
        if restored:
            decision, reason = 'resolved', '原失败步骤已通过核验，返回原节点继续。'
        elif hard_reason:
            decision, reason, trigger = 'converge', hard_reason, 'hard_gate'
        elif progress:
            decision, reason = 'continue', '已核验的阻塞减少或原步骤取得有效结果，继续推进目标。'
        elif repeated:
            decision, reason = 'converge', '同一处理方法再次没有解除阻塞，先更新诊断与具体出路。'
        elif self.state['stagnation'] >= 2:
            decision, reason = 'converge', '连续自检没有取得可核验进展，保留检查点并汇报可选出路。'
        else:
            decision, reason = 'review', '尚无可核验进展；先分析一个具体的新假设，再决定是否验证。'
        check = {'identity': identity, 'trigger': trigger, 'decision': decision, 'reason': reason,
                 'method': method, 'evidence_refs': evidence, 'seconds': seconds,
                 'active_seconds': self.state['active_seconds']}
        self.state['checks'].append(check)
        self.state['consumed_evidence'] = sorted(seen | set(evidence))
        self.state['needs_review'] = decision == 'review' or trigger == 'overdue'
        return check

    def assess_plan(self, plan, option):
        """The next normal analyst response is the model's safe-point review."""
        if not self.state['needs_review']:
            return None
        review = plan.get('boundary_review') or {}
        if review.get('action') == 'converge':
            return review.get('reason') or '分析认为当前无法继续推进，请查看诊断。'
        strategy = option.get('strategy') or {}
        method = digest([strategy.get('kind'), sorted(strategy.get('tools', [])), strategy.get('target')])
        latest = self.state['checks'][-1] if self.state['checks'] else {}
        # Legacy plans carry a concrete strategy but no new review field.
        if not strategy.get('target') or not strategy.get('tools'):
            return '未提供可核验的下一步方法，当前进度已保留。'
        if method == latest.get('method') and not latest.get('evidence_refs'):
            return '拟议方法与刚才失败的方法相同，且没有新证据支持重试。'
        if review and (not review.get('reason') or not review.get('next_step')):
            return '自检缺少具体下一步，当前进度已保留。'
        self.state['needs_review'] = False
        self.state['last_review'] = copy.deepcopy(review or {
            'action': 'continue', 'reason': option.get('method', ''), 'next_step': strategy['target']})
        return None


def host_progress(before_blockers, after_blockers, *, operation_restored, probe):
    """Called only after the runtime re-reads the host ledger and probe."""
    refs = ['cleared:' + identity for identity in sorted(set(before_blockers) - set(after_blockers))]
    if operation_restored and probe.get('passed') is True:
        refs.extend('original_step:' + str(ref) for ref in probe.get('evidence_refs', []) if ref)
    return refs


def milestone_files(goal, work_dir):
    """Observe declared workspace artifacts, never arbitrary evidence prose.

    This proves a material change, not correctness or business acceptance.
    Resolved paths must remain inside the existing workspace; no external reads.
    """
    from pathlib import Path
    import hashlib
    if not work_dir:
        return {}
    root = Path(work_dir).resolve()
    observed = {}
    for milestone in (goal or {}).get('milestones', []):
        evidence = milestone.get('evidence', '')
        if not isinstance(evidence, str) or not evidence.startswith('file:'):
            continue
        try:
            path = (root / evidence[5:].strip()).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
            before = path.stat()
            if before.st_size == 0:
                continue
            sha = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    sha.update(block)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                continue
            observed[str(path)] = {'digest': sha.hexdigest(), 'milestone_id': milestone['id']}
        except (OSError, ValueError, KeyError):
            continue
    return observed


def milestone_progress(before, after):
    # One artifact is a milestone once per case. Rewriting it or renaming the
    # model's milestone id cannot manufacture repeated progress.
    return ['artifact:' + path for path, value in after.items()
            if before.get(path, {}).get('digest') != value['digest']]
