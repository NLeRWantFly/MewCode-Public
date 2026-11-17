"""Deterministic role bindings drawn only from the configured provider catalog."""
from mewcode.model_registry import ModelRef

ROLES = ('planner', 'executor', 'verifier', 'reviewer', 'arbiter', 'coordinator')
ALIASES = {'manager': 'planner', 'planner': 'planner', 'executor': 'executor', 'auditor': 'verifier'}


class RoleModels:
    def __init__(self, catalog):
        self.rows = {}
        for row in catalog.get('models') or []:
            if not row.get('available') or row.get('review_only') or row.get('protocol') == 'typesafe':
                continue
            try:
                ref = ModelRef.from_value(row)
            except ValueError:
                continue
            self.rows[(ref.provider_key, ref.model_id)] = dict(row)

    def require(self, value, role):
        if role not in ROLES:
            raise ValueError('未定义的模型角色')
        ref = ModelRef.from_value(value)
        row = self.rows.get((ref.provider_key, ref.model_id))
        if row is None or role in {'planner', 'executor', 'verifier'} and row.get('tools') is False:
            raise ValueError(f'{role} 所选模型不在当前可用模型池中')
        efforts = row.get('efforts') or []
        if ref.effort and efforts and ref.effort not in efforts:
            raise ValueError(f'{role} 所选推理档位不可用')
        return ref

    def candidates(self, role):
        result = []
        for key in sorted(self.rows):
            row = self.rows[key]
            try:
                result.append(self.require({**row, 'effort': row.get('default_effort') or ''}, role))
            except ValueError:
                continue
        return result

    def vendor(self, ref):
        row = self.rows[(ref.provider_key, ref.model_id)]
        return str(row.get('provider_brand') or row.get('brand') or ref.provider_key).casefold()

    def independent(self, executor, verifier):
        return executor.provider_key != verifier.provider_key and self.vendor(executor) != self.vendor(verifier)

    def bind(self, profile, overrides=None):
        overrides = {**{role: ref.as_dict() for role, ref in getattr(profile, 'role_models', {}).items()}, **(overrides or {})}
        if set(overrides) - set(ROLES):
            raise ValueError('模型绑定包含未知角色')
        preferred = {'planner': getattr(profile, 'planner', None) or profile.manager, 'executor': profile.executor,
                     'verifier': profile.auditor, 'reviewer': profile.auditor, 'arbiter': profile.manager,
                     'coordinator': profile.manager}
        bindings = {role: self.require(overrides.get(role) or preferred[role], role) for role in ROLES}
        executor = bindings['executor']
        if 'verifier' not in overrides and not self.independent(executor, bindings['verifier']):
            choices = self.candidates('verifier')
            preferred_row = self.rows[(bindings['verifier'].provider_key, bindings['verifier'].model_id)]
            if preferred_row.get('vision') is True:
                choices = [ref for ref in choices if self.rows[(ref.provider_key, ref.model_id)].get('vision') is True]
            different = [ref for ref in choices if self.independent(executor, ref)]
            different_model = [ref for ref in choices if ref.model_id != executor.model_id]
            if different or different_model:
                bindings['verifier'] = (different or different_model)[0]
        independent = self.independent(executor, bindings['verifier'])
        return {'roles': {role: ref.as_dict() for role, ref in bindings.items()},
                'explicit_roles': sorted(overrides), 'verification_independent': independent,
                'notice': '' if independent else '核查不独立', 'schema_version': 1}

    def alternate(self, binding, role):
        current = self.require(binding['roles'][role], role)
        candidates = [ref for ref in self.candidates(role)
                      if (ref.provider_key, ref.model_id) != (current.provider_key, current.model_id)]
        candidates.sort(key=lambda ref: (ref.provider_key == current.provider_key, ref.provider_key, ref.model_id))
        return candidates[0] if candidates else None


def rebind_role(record, *, pool, role, model, source_ref):
    """Only a host UI or a reserved alternate-route attempt calls this."""
    ref = pool.require(model, role)
    previous = next((row for row in record.get('role_model_changes', []) if row['source_ref'] == source_ref), None)
    if previous:
        if previous['role'] != role or previous['after'] != ref.as_dict():
            raise ValueError('同一模型变更来源不能改写选择')
        return False
    binding = record['role_models']
    old = binding['roles'][role]
    if old == ref.as_dict():
        return False
    binding['roles'][role] = ref.as_dict()
    if role == 'coordinator' and record.get('collaboration'):
        from mewcode.kernel.collaboration import takeover
        takeover(record['collaboration'], ref.as_dict(), source_ref=source_ref)
    binding['verification_independent'] = pool.independent(
        ModelRef.from_value(binding['roles']['executor']), ModelRef.from_value(binding['roles']['verifier']))
    binding['notice'] = '' if binding['verification_independent'] else '核查不独立'
    record.setdefault('role_model_changes', []).append({'role': role, 'before': old, 'after': ref.as_dict(), 'source_ref': source_ref})
    return True
