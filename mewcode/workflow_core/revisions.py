"""Copy-on-write output areas for a node's amendments, with stable file names."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import shutil


def prepare(work_dir, run_id, request, previous_delivery, artifact_root):
    project = Path(work_dir).resolve()
    identity = hashlib.sha256((request.get('working_copy_id') or request['id']).encode()).hexdigest()[:24]
    root = (project / '.mewcode' / 'workflow-revisions' / run_id / identity).resolve()
    root.mkdir(parents=True, exist_ok=True)
    allowed = (Path(artifact_root) / run_id).resolve()
    mapping = {}
    for artifact in previous_delivery.get('artifacts', []):
        source = Path(artifact.get('stored_path') or '').resolve()
        if not source.is_file() or not source.is_relative_to(allowed):
            continue
        original = Path(artifact.get('source_path') or artifact.get('path') or artifact['name'])
        logical = artifact.get('logical_path')
        relative = Path(logical) if logical and not Path(logical).is_absolute() else None
        if relative is None:
            relative = original.relative_to(project) if original.is_absolute() and original.is_relative_to(project) else Path(original.name)
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Resume keeps partially edited output; never recopy over a checkpoint.
        # A late reply can require no file changes at all. Keep its inputs as
        # frozen read references instead of copying an entire previous delivery.
        if request.get('kind') != 'update' and not target.exists():
            shutil.copyfile(source, target)
        mapping[str(target)] = artifact.get('logical_path') or artifact.get('source_path') or artifact.get('path')
    return root, mapping


def immutable_inputs(value, allowed):
    paths = set()
    if isinstance(value, dict):
        if value.get('stored_path'):
            candidate = Path(value['stored_path']).resolve()
            if candidate.is_file() and candidate.is_relative_to(allowed):
                paths.add(str(candidate))
        for item in value.values():
            paths.update(immutable_inputs(item, allowed))
    elif isinstance(value, list):
        for item in value:
            paths.update(immutable_inputs(item, allowed))
    return paths


def grant_reads(agent, value, allowed):
    """New replies can introduce snapshots after the Agent has already started."""
    paths = immutable_inputs(value, allowed)
    sandbox = agent.permission_checker.sandbox
    if hasattr(sandbox, 'grant_approved_path'):
        for path in paths:
            sandbox.grant_approved_path(path, read_only=True)
    if paths and agent.registry.is_enabled('ReadAuditArtifact'):
        reader = copy.copy(agent.registry.get('ReadAuditArtifact'))
        reader.roots = list(dict.fromkeys([*reader.roots, *(Path(path) for path in sorted(paths))]))
        agent.registry.register(reader)
