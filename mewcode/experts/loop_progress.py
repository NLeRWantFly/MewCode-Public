"""Stable round evidence from host-fixed deliveries, separate from review prose."""
from __future__ import annotations


def artifact_versions(record, node_id):
    """Project file versions only from the current host certificate."""
    from mewcode.kernel.collaboration import valid_certificate

    value = record.get('collaboration') or {}
    if not (value.get('certificates') or {}).get(node_id) or not valid_certificate(value, node_id):
        return None
    cert = value['certificates'][node_id]
    rows = cert.get('artifacts') or []
    return sorted(({'path': row['path'], 'sha256': row['sha256']}
                   for row in rows if row.get('kind') == 'file'), key=lambda row: row['path'])


def round_progress(local, contract, reports):
    """Changing an explanation cannot advance unchanged production artifacts.

    Queue counts retain their existing meaning. Ordinary delivery loops use
    fixed producer bytes plus criterion states; reviewer report rewrites and
    new attempt IDs do not buy another round. Inline-only legacy flows keep
    the original located-evidence comparison.
    """
    if all(local[key].review_progress for key in contract.reviewer_node_ids):
        return {key: {field: local[key].review_progress.get(field) for field in
                ('manifest_sha256', 'total', 'pending', 'complete', 'needs_repair', 'unknown')}
                for key in contract.reviewer_node_ids}
    files = {}
    for key in contract.node_ids:
        if key in contract.reviewer_node_ids or key not in local:
            continue
        observed = (local[key].work_progress or {}).get('artifact_versions')
        if observed:
            files[key] = observed
    if not files:
        return None
    return {'schema_version': 1, 'producer_files': files,
            'criteria': {key: sorted(({'criterion_id': row['criterion_id'], 'state': row['state']}
                                     for row in report.get('checks') or []),
                                    key=lambda row: row['criterion_id'])
                         for key, report in reports.items()}}
