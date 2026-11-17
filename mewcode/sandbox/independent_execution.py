"""Dependency checks for new offline, disposable work beside deferred cleanup.

The old operation keeps its outcome and cleanup state. Only its frozen private
copy can be left behind; new work gets a different copy and AppContainer SID.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def private_cleanup_dependency(record: dict, workspace: Path, command: str) -> dict | None:
    from .lpac_runtime import MAX_REQUEST, checked_path
    from .transaction_registry import private_candidate_only

    if record.get('state') != 'cleanup_unknown' or not private_candidate_only(record) or not command.strip():
        return None
    try:
        source = checked_path(workspace)
        private = checked_path(Path(record['transaction']))
        path = checked_path(private / 'cleanup/request.bin', file=True)
        with path.open('rb') as stream:
            raw = stream.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST or hashlib.sha256(raw).hexdigest() != record['request_sha256']:
            return None
        envelope = json.loads(raw)
        request = envelope['request']
        candidate = checked_path(Path(request['workspace']))
        if (envelope['schema'] != 'mewcode-lpac-staged-request-v1'
                or envelope['request_id'] != record['request_id']
                or request['request_id'] != record['request_id']
                or checked_path(Path(envelope['original_workspace'])) != source
                or candidate == private or not candidate.is_relative_to(private)
                or request['schema'] not in {'mewcode-lpac-request-v1', 'mewcode-lpac-network-request-v1'}):
            return None
        # A network job may retain its shared network SID. New work is strictly
        # offline and receives a fresh SID, so it never grants that old SID any
        # new capability. Check that the old request cannot reach the source or
        # another candidate, including through a shared staging ancestor.
        reads, writes = request['read_paths'], request['write_paths']
        if not isinstance(reads, list) or not isinstance(writes, list) or not reads:
            return None
        for value in reads + writes:
            target = checked_path(Path(value))
            if (target == source or target.is_relative_to(source) or source.is_relative_to(target)
                    or private.parent.is_relative_to(target)
                    or (target.is_relative_to(private.parent) and not target.is_relative_to(private))):
                return None
        if any(not checked_path(Path(value)).is_relative_to(private) for value in writes):
            return None
        arguments = request['arguments']
        if arguments[-2] != '-Command' or not isinstance(arguments[-1], str):
            return None
        old = arguments[-1].replace('\r\n', '\n').strip()
        new = command.replace('\r\n', '\n').strip().removesuffix('; exit $LASTEXITCODE').rstrip()
        if any(script.endswith(new) for script in (old, old.removesuffix('; exit $LASTEXITCODE').rstrip())):
            return None
        return {key: record[key] for key in ('id', 'request_id', 'request_sha256')}
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
        return None
