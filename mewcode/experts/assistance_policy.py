"""Additional audit-output protection; all calls still use the parent policy."""
from mewcode.permissions.checker import Decision
from mewcode.permissions.effects import EffectSignature, effect_signature


def attach_assistance_checks(checker, policy, work_dir):
    original = checker.check
    protected = [effect_signature('WriteFile', {'file_path': p}, work_dir=work_dir)
                 for p in policy.assistance_protected_paths]
    def check(tool, arguments):
        decision = original(tool, arguments)
        effect = effect_signature(tool.name, arguments, work_dir=work_dir, tool=tool)
        if protected and effect.kind != 'read' and any(effect.overlaps(path) for path in protected):
            decision = Decision('deny', '审查协助保护被审成果：请将修复建议回交执行节点。', source='assistance_audit')
        if policy.assistance_permission_log is not None:
            policy.assistance_permission_log.append({'tool': tool.name, 'summary': decision.reason,
                'receipt_id': None, 'permission_decision': str(decision.effect) + ': ' + decision.reason})
        return decision
    checker.check = check
