"""Explicit, one-shot host confirmation for an immutable recovery journal.

This never grants execution or changes the original task/policy/manifest. A
confirmation only permits checking a committed result or rolling back the
listed entries under both their frozen and current write scopes.
"""
from __future__ import annotations

import re
import uuid

from mewcode.sandbox import staged_workspace as stage
from mewcode.sandbox import staged_publication as publication
from mewcode.sandbox.transaction_registry import CURRENT_TRANSACTION, TERMINAL
from pathlib import Path


def preview_locked(scope, record):
    from mewcode.sandbox.staged_execution import policy_revision
    if (record["state"] in TERMINAL or Path(record["workspace"]) != scope.workspace
            or not scope.current()):
        raise stage.StagingRejected("Recovery needs a pending transaction and current workspace authority")
    targets, journal_sha = [], None
    if record.get("transaction"):
        plan = publication.restore_staged_plan(Path(record["transaction"]), source=scope.workspace,
                                                manifest_sha256=record["manifest_sha256"])
        if plan.policy_revision != record["policy_revision"]:
            raise stage.StagingRejected("Original recovery policy binding changed")
        path = plan.transaction / "publication/plan.json"
        if path.exists():
            journal, raw = publication._load(path)
            if (journal.get("source") != str(scope.workspace)
                    or journal.get("holder") != record.get("holder")
                    or journal.get("policy_revision") != plan.policy_revision
                    or journal.get("manifest_sha256") != plan.manifest_sha256):
                raise stage.StagingRejected("Recovery preview does not match the original host record")
            entries = journal.get("entries")
            if not isinstance(entries, list) or not 0 < len(entries) <= plan.max_entries:
                raise stage.StagingRejected("Invalid recovery preview entries")
            baseline = {entry.relative: entry for entry in plan.baseline}
            for entry in entries:
                relative = publication._relative(entry["relative"])
                old = baseline.get(relative)
                if (entry.get("operation") not in {"create", "modify", "delete"}
                        or (entry["operation"] == "create") != (old is None)
                        or old is not None and not old.writable):
                    raise stage.StagingRejected("Recovery preview exceeds the frozen write scope")
                targets.append(relative)
            if len(set(targets)) != len(targets):
                raise stage.StagingRejected("Duplicate recovery preview target")
            journal_sha = stage._hash(raw)
    if any(not scope.allows(scope.workspace / target, write=True) for target in targets):
        raise stage.StagingRejected("Current task does not authorize every recovery target")
    payload = {"schema": "mewcode-recovery-handoff-v1", "transaction_id": record["id"],
        "record_sha256": stage._hash(stage._encoded(record)), "journal_sha256": journal_sha,
        "manifest_sha256": record.get("manifest_sha256"), "workspace": str(scope.workspace),
        "original_task_id": record["task_id"], "current_task_id": scope.task_id,
        "original_policy_revision": record["policy_revision"], "current_policy_revision": policy_revision(scope),
        "action": "verify_committed_or_rollback_unfinished", "targets": sorted(targets)}
    return {**payload, "token": stage._hash(stage._encoded(payload))}


def preview(scope, registry, transaction_id):
    if not isinstance(transaction_id, str) or not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
        raise stage.StagingRejected("Invalid recovery transaction identity")
    with registry.claim(transaction_id):
        token = CURRENT_TRANSACTION.set(transaction_id)
        try:
            return preview_locked(scope, registry.get(transaction_id))
        finally:
            CURRENT_TRANSACTION.reset(token)


def confirm_locked(scope, record, confirmation):
    if not isinstance(confirmation, dict) or confirmation.get("transaction_id") != record["id"]:
        raise stage.StagingRejected("Recovery confirmation belongs to another transaction")
    current = preview_locked(scope, record)
    if confirmation.get("token") != current["token"]:
        raise stage.StagingRejected("Recovery preview changed; review and confirm the new scope")
    return current


def record_confirmation(registry, handoff):
    """Append a new immutable receipt, never overwrite previous decisions."""
    from mewcode.sandbox.lpac_runtime import checked_path
    directory = registry.root / "recovery-confirmations"
    directory.mkdir(exist_ok=True)
    checked_path(directory)
    destination = directory / (handoff["token"] + ".json")
    raw = stage._encoded(handoff)
    def existing_receipt():
        _, saved = publication._load(destination)
        if saved != raw:
            raise stage.StagingRejected("Recovery confirmation receipt changed")
        return str(destination)
    if destination.exists():
        return existing_receipt()
    # A process lost during write must leave only an unreferenced temporary
    # file, never a truncated final receipt that permanently poisons a token.
    temporary = directory / (handoff["token"] + "." + uuid.uuid4().hex + ".pending")
    stage._write_new(temporary, raw)
    try:
        from mewcode.sandbox.publication_io import PinnedPath
        with PinnedPath(temporary, movable=True) as pinned:
            pinned.rename_absent(destination)
    except FileExistsError:
        # A crash after append but before the ledger link may be retried; do
        # not overwrite a different receipt or create a permanent dead end.
        return existing_receipt()
    return str(destination)
