"""Bounded host recovery of previously enabled, fully attested runtimes.

Models may request diagnosis/recovery, but cannot create this host opt-in or
select a bundle, machine, evidence, permission mode or recovery action.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from mewcode.sandbox import native_gate as gate
from mewcode.sandbox.windows import NativeBoundaryUnavailable

POLICY_NAME = "recovery-policy.json"
POLICY_SCHEMA = "mewcode-native-recovery-v1"
ACTION = "restore_enablement"


def _root(bundle_root: Path | None) -> Path:
    return Path(bundle_root) if bundle_root is not None else gate._default_bundle_root()


@contextmanager
def host_gate_lock(root: Path):
    """Serialize opt-in, explicit disable and repair across host processes."""
    from mewcode.sandbox.lpac_runtime import checked_path
    from mewcode.sandbox.transaction_registry import TransactionRegistry
    root = checked_path(root)
    identity = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:32]
    with TransactionRegistry(root.parent).claim(identity):
        yield root


def _unique(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Duplicate recovery policy field")
        data[key] = value
    return data


def _policy(root: Path, status: gate.GateStatus) -> dict:
    from mewcode.sandbox.lpac_runtime import checked_path
    path = checked_path(root / POLICY_NAME, file=True)
    if path.stat().st_size > gate.MAX_RECORD_BYTES:
        raise ValueError("Recovery policy exceeds bound")
    record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
    if (not isinstance(record, dict) or record.get("schema") != POLICY_SCHEMA
            or record.get("allowed_actions") != [ACTION]
            or not gate._bound(record, status.bundle_identity, gate.machine_guid())):
        raise ValueError("Recovery policy does not authorize this verified bundle and machine")
    return record


def _write(root: Path, name: str, record: dict):
    from mewcode.sandbox.lpac_runtime import checked_path
    checked_path(root)
    target = root / name
    if os.path.lexists(target):
        checked_path(target, file=True)
    payload = (json.dumps(record, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    if len(payload) > gate.MAX_RECORD_BYTES:
        raise ValueError("Recovery record exceeds bound")
    temporary = root / ("." + name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def authorize_recovery(bundle_root: Path, *, note: str = "user") -> dict:
    """Installer-only host operation after explicit user authorization."""
    with host_gate_lock(bundle_root) as root:
        status = gate.native_gate_status(root)
        if not status.ready:
            raise ValueError("Recovery opt-in requires an already enabled, fully attested runtime")
        _write(root, POLICY_NAME, {
            "schema": POLICY_SCHEMA, "bundle_identity": status.bundle_identity,
            "machine_guid": gate.machine_guid(), "allowed_actions": [ACTION],
            "authorized_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "operator": note.strip()[:300] or "user",
        })
        return diagnose_native_runtime(root)


def revoke_recovery(root: Path):
    """Caller must hold host_gate_lock before closing the gate."""
    from mewcode.sandbox.lpac_runtime import checked_path
    path = root / POLICY_NAME
    if os.path.lexists(path):
        checked_path(path, file=True).unlink()


def diagnose_native_runtime(bundle_root: Path | None = None) -> dict:
    root = _root(bundle_root)
    status = gate.native_gate_status(root)
    authorized = False
    try:
        _policy(root, status)
        authorized = True
    except (OSError, ValueError, RecursionError, NativeBoundaryUnavailable):
        pass
    return {"ready": status.ready, "gate_state": status.state.value,
            "reason": status.reason, "missing_evidence": list(status.missing_evidence),
            "recovery_authorized": authorized,
            "recoverable": authorized and status.state is gate.NativeGateState.ELIGIBLE,
            "command_started": False, "action": "inspected"}


def recover_native_runtime(bundle_root: Path | None = None) -> dict:
    """One repair attempt; never install, attest, grant scope or retry a command."""
    root = _root(bundle_root)
    initial = diagnose_native_runtime(root)
    if initial["ready"] or not initial["recoverable"]:
        return {**initial, "action": "already_ready" if initial["ready"] else "blocked", "changed": False}
    try:
        with host_gate_lock(root) as checked:
            status = gate.native_gate_status(checked)
            if status.ready:
                return {**diagnose_native_runtime(checked), "action": "already_ready", "changed": False}
            if status.state is not gate.NativeGateState.ELIGIBLE:
                return {**diagnose_native_runtime(checked), "action": "blocked"}
            _policy(checked, status)  # Re-read opt-in under the shared disable lock.
            _write(checked, gate.ENABLEMENT_NAME, {
                "schema": gate.ENABLEMENT_SCHEMA, "bundle_identity": status.bundle_identity,
                "machine_guid": gate.machine_guid(),
                "enabled_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "operator": "host authorized automatic recovery",
            })
            final = diagnose_native_runtime(checked)  # All evidence is checked again.
            return {**final, "action": ACTION if final["ready"] else "blocked", "changed": final["ready"] is True}
    except (OSError, ValueError, NativeBoundaryUnavailable) as error:
        return {**initial, "ready": False, "action": "blocked", "reason": str(error),
                "error_code": "native_recovery_blocked"}


def recover_unpublished_transactions(checker) -> list[dict]:
    """Settle interrupted private candidates without requiring old task grants.

    Never replay commands or recover a mutated publication here. This path
    cannot start the privileged publisher or expand/reconstruct lost grants.
    """
    from mewcode.sandbox.lpac_runtime import NativeScope
    from mewcode.sandbox.staged_execution import reconcile
    from mewcode.sandbox.transaction_registry import TransactionRegistry
    registry = TransactionRegistry()
    outcomes = []
    try:
        records = registry.pending(checker.sandbox.project_root)
        # Admission permits only one pending transaction per workspace.
        if len(records) > 1:
            return [{"state": "recovery_required", "reason": "Conflicting pending host transactions"}]
        for record in records:
            mode = record.get("publication_mode", "publish")
            scope = NativeScope.from_checker(checker, publication_mode=mode)
            outcomes.extend(reconcile(scope, registry, allow_publication_recovery=False))
    except (OSError, ValueError, RuntimeError) as error:
        outcomes.append({"state": "recovery_required", "reason": str(error)})
    return outcomes


def restore_cleanup_owner(owner, checker) -> bool:
    """Release an in-memory fence only from settled, bound host cleanup facts."""
    from mewcode.sandbox.lpac_runtime import NativeScope
    from mewcode.sandbox.transaction_registry import TERMINAL, TransactionRegistry, workspace_write_block
    identities = getattr(owner, "_native_cleanup_transaction_ids", None)
    if not identities or checker is None or getattr(owner, "_native_cleanup_unbound", False):
        return False
    try:
        recover_unpublished_transactions(checker)
        if workspace_write_block(checker.sandbox.project_root):
            return False
        registry = TransactionRegistry()
        for identity in identities:
            record = registry.get(identity)
            scope = NativeScope.from_checker(checker, publication_mode=record.get("publication_mode", "publish"))
            if (record["state"] not in TERMINAL or record.get("workspace") != str(scope.workspace)
                    or (record.get("native_receipt") or {}).get("cleanup_confirmed") is not True):
                return False
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError):
        return False
    owner._native_cleanup_error = ""
    owner._native_cleanup_transaction_ids = set()
    return True


def cleanup_blocks_project(owner, checker) -> bool:
    """Keep uncertain command cleanup local to commands when source isolation is known."""
    from mewcode.sandbox.transaction_registry import TransactionRegistry, private_candidate_only, workspace_key
    identities = getattr(owner, "_native_cleanup_transaction_ids", None)
    if not identities or checker is None or getattr(owner, "_native_cleanup_unbound", False):
        return True
    try:
        registry = TransactionRegistry()
        workspace = workspace_key(checker.sandbox.project_root)
        for identity in identities:
            record = registry.get(identity)
            if workspace_key(Path(record["workspace"])) != workspace or not private_candidate_only(record):
                return True
        return False
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError):
        return True
