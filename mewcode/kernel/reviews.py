"""Run-bound safety verdicts; source authority and target facts bind every reuse."""
from dataclasses import asdict

from mewcode.kernel.events import digest


class ReviewCache:
    def __init__(self, store, run_id, scope):
        self.store, self.run_id, self.scope = store, run_id, scope

    def key(self, action_key, manifest):
        return digest([self.scope, action_key, manifest])

    def get(self, key):
        from mewcode.permissions.reviewer import SafetyReviewResult
        row = (self.store.read_execution_fields(self.run_id, 'kernel_review_verdicts').get('kernel_review_verdicts') or {}).get(key)
        return SafetyReviewResult(**row, cache_hit=True) if row else None

    def put(self, key, result):
        if result.unavailable or result.inconclusive or not result.reason.strip():
            return
        row = {name: value for name, value in asdict(result).items()
               if name not in {'execution_binding', 'cache_hit'}}
        self.store.mutate(self.run_id, lambda record: record.setdefault('kernel_review_verdicts', {}).__setitem__(key, row), durable=True)


def authorization_manifest(record):
    declared = record.get('authorization_manifest')
    if declared is not None:
        return declared
    # Transitional typed authority, until capability compilation supplies the
    # full manifest. Neither model prose nor chat history creates grants.
    return {'schema_version': 1, 'goal': record.get('task', ''),
            'entries': (record.get('authorizations') or {}).get('entries', []),
            'scope': record.get('host_execution_scope') or {}}
