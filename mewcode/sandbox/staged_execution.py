"""The production copy → native execution → verify → publish lifecycle.

This module does not decide whether AUTO may start. The caller must pass the
production gate or a separately bounded synthetic acceptance entry. No
worker-provided target, hash, policy or success marker is accepted as authority.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import copy
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import re
import time
import weakref

from mewcode.sandbox import staged_workspace as stage
from mewcode.sandbox import staged_publication as publication
from mewcode.sandbox import lpac_runtime as lpac
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.sandbox.transaction_registry import CURRENT_TRANSACTION, TransactionRegistry, NativeTransactionBusy
from mewcode.orchestration.write_lease import WriteLeaseStore, WriteLeaseError

PROTOCOL = "mewcode-lpac-staged-request-v1"
_BROKER_LOCKS = weakref.WeakKeyDictionary()


def policy_revision(scope) -> str:
    """Snapshot actual host authority, including on-disk rules and node scope."""
    from mewcode.experts.recovery_scope import current_approved_revision
    checker = scope.checker
    rules = checker.rule_engine
    prefixes = rules._load_command_prefix_rules()
    sandbox = checker.sandbox
    value = {"task": scope.task_id, "mode": checker.mode.value,
             "publication_mode": getattr(scope, "publication_mode", "publish"),
             "independent_offline": getattr(scope, "independent_offline", False),
             "host_publication_mode": getattr(checker, "native_publication_mode", "publish"),
             "workspace": str(scope.workspace),
             "authorization": checker.task_authorization.as_dict() if checker.task_authorization else None,
             "roots": [str(p) for p in sandbox._allowed_roots],
             "files": sandbox.file_grants, "directories": sandbox.dir_grants,
             "rules": [[asdict(r) for r in tier] for tier in [rules._session_rules, *rules._load_tiers()]],
             "prefixes": [asdict(r) for r in prefixes.rules],
             "issues": [asdict(r) for r in rules.command_rule_issues],
             "approved_calls": sorted(checker._approved_calls),
             "node": current_approved_revision()}
    from mewcode.permissions.network_policy import policy_for
    value['network_policy'] = policy_for(checker).as_dict()
    if checker.grant_store is not None:
        value["grants"] = [asdict(g) for g in checker.grant_store.list_grants()]
    return hashlib.sha256(stage._encoded(value)).hexdigest()


class MappedScope:
    def __init__(self, original, workspace):
        self.original, self.workspace = original, workspace

    def current(self):
        return self.original.current()

    def allows(self, path, *, write):
        if path.is_relative_to(self.workspace):
            path = self.original.workspace / path.relative_to(self.workspace)
        return self.original.allows(path, write=write)


def protections(plan):
    readonly, containers = [], []
    ancestors = {p.as_posix() for e in plan.baseline if e.writable for p in Path(e.relative).parents}
    for entry in plan.baseline:
        if entry.relative == "." or entry.writable:
            continue
        path = plan.workspace / entry.relative
        if entry.kind == "directory" and (path.name.casefold() == ".git" or entry.relative in ancestors):
            containers.append(str(path))
        else:
            readonly.append(str(path))
    return readonly, containers


def staging_parent(source: Path, registry: TransactionRegistry, *, configured: Path | None = None) -> Path:
    """Select a same-volume private store, never silently use a volume root.

    For other volumes a host-configured existing directory is required; no
    implicit directory creation beside an unrelated user's project.
    """
    if configured is None:
        from mewcode.sandbox.volume_stores import resolve
        configured = resolve(registry, source)
        if configured is None:
            if source.stat().st_dev != registry.root.stat().st_dev:
                raise stage.StagingRejected("Configure a private staging directory on the workspace volume")
            configured = registry.root / "staging"
            configured.mkdir(exist_ok=True)
    parent = lpac.checked_path(configured)
    if (not parent.is_dir() or parent.stat().st_dev != source.stat().st_dev
            or parent == source or parent.is_relative_to(source) or source.is_relative_to(parent)
            or parent == Path(parent.anchor)):
        raise stage.StagingRejected("Private publication storage must be disjoint and on the source volume")
    return parent


def lease_scopes(plan, changes):
    """Lease exactly the changed entries, including not-yet-created parents.

    A directory entry is a point, NOT a subtree authorization. The publisher
    still rechecks every frozen/current permission and every target handle.
    """
    return {"entry_paths": sorted({str(plan.source / change.relative) for change in changes})}


def _leases(registry, scope):
    return WriteLeaseStore(registry.root / "publication-leases.json", sandbox=scope.checker.sandbox)


def metadata_inventory(plan):
    from mewcode.sandbox.workspace_metadata import capture_metadata
    sources, candidates, size = {}, {}, 0
    for entry in plan.baseline:
        source = capture_metadata(plan.source / entry.relative)
        candidate = capture_metadata(plan.workspace / entry.relative)
        size += len(stage._encoded([entry.relative, source, candidate]))
        if size > publication.MAX_JOURNAL_BYTES:
            raise stage.StagingRejected("Metadata inventory exceeds host bound; split the workspace")
        sources[entry.relative], candidates[entry.relative] = source, candidate
    data = stage._encoded({"sources": sources, "candidates": candidates})
    stage._write_new(plan.transaction / "metadata.json", data)
    return {"sources": sources, "candidates": candidates}, stage._hash(data)


def metadata_preflight(plan, inventory, changes):
    """No replacement when original metadata is unknown or would be lost.

    This is intentionally not advertised as universal Windows metadata
    support: unreadable SACLs stay a typed compatibility blocker.
    """
    from mewcode.sandbox.workspace_metadata import capture_metadata, require_supported_metadata, metadata_drift
    changed = {c.relative for c in changes}
    # Catch metadata-only candidate changes too: byte-identical output is not
    # evidence that chmod/timestamps/streams have been faithfully published.
    for relative, before in inventory["candidates"].items():
        path = plan.workspace / relative
        if relative in changed or not path.exists():
            continue
        now = capture_metadata(path)
        fields = ("attributes", "created", "security", "streams", "extended_attributes")
        if before["kind"] == "file":
            fields += ("written",)
        if any((_candidate_security(before[field]) != _candidate_security(now[field])) if field == "security"
               else before.get(field) != now.get(field) for field in fields):
            raise stage.StagingRejected("Metadata-only candidate change requires an explicit supported publisher")
    if not changed:
        return
    # Every original and destination ancestor participating in replacement
    # must have complete metadata; an unknown SACL is not presumed empty.
    needed = set(changed)
    needed.update(p.as_posix() for name in changed for p in Path(name).parents)
    for relative in sorted(needed):
        before = inventory["sources"].get(relative)
        if before is None:
            continue
        require_supported_metadata(before)
        now = capture_metadata(plan.source / relative)
        require_supported_metadata(now)
        if metadata_drift(before, now):
            raise stage.StagingRejected("Original metadata drifted before publication")
    for change in changes:
        if change.operation != "delete":
            require_supported_metadata(capture_metadata(plan.workspace / change.relative))


def _candidate_security(value):
    # Only the disposable private copy: adding/removing its inherited LPAC
    # grant materializes AI/ID flags on existing ACEs. Preserve ACE order,
    # rights, SID, propagation flags and protected DACL flag P. Never apply
    # this normalization to original workspace CAS or publication security.
    result = copy.deepcopy(value)
    section = result.get("owner_group_dacl", {})
    sddl = section.get("sddl")
    if isinstance(sddl, str):
        sddl = re.sub(r"D:([^()]*)", lambda m: "D:" + m.group(1).replace("AI", ""), sddl)
        sddl = re.sub(r"\(([^;()]*);([^;()]*);", lambda m:
                      "(" + m.group(1) + ";" + m.group(2).replace("ID", "") + ";", sddl)
        section["sddl"] = sddl
    sacl = result.get("sacl", {})
    # A file recreated by the restricted child (e.g. Git index.lock -> index)
    # can have a present but zero-ACE audit ACL where the initial disposable
    # copy had none. Only these exact unprotected, captured empty forms have
    # no audit rules to lose. NULL, protected, unknown and every actual ACE
    # remain distinct. Originals and final publication stay byte-policy exact.
    if (sacl.get("state") == "captured" and sacl.get("present") is True
            and sacl.get("null") is False and sacl.get("sddl") in {"S:", "S:AI"}):
        sacl.update(present=False, null=False, sddl=None)
    return result


async def execute_staged(command: str, *, scope, helper: Path, timeout: int,
                         registry: TransactionRegistry | None = None, host_parent: Path | None = None,
                         extra_read=(), on_receipt=None, runner=None, request_transform=None,
                         runtime_git_dir: Path | None = None) -> NativeExecutionReceipt:
    from mewcode.sandbox.metadata_broker import installed_artifact
    publication_mode = getattr(scope, "publication_mode", "publish")
    if publication_mode not in {"publish", "discard"}:
        raise stage.StagingRejected("Invalid host publication mode")
    if runtime_git_dir is not None:
        from mewcode.sandbox.network_git import paths as verified_network_git_paths, runtime_git_directory
        if request_transform is None or lpac.checked_path(runtime_git_dir) != runtime_git_directory():
            raise stage.StagingRejected("Network Git must use the verified host runtime package")
        verified_network_git_paths()
    # An observation never enters the privileged publisher. Its disposable
    # copy still uses the native gate, exact host scope and cleanup proof.
    artifact = None if publication_mode == "discard" else installed_artifact(helper)
    arguments = dict(scope=scope, helper=helper, timeout=timeout, registry=registry,
                     host_parent=host_parent, extra_read=extra_read, on_receipt=on_receipt,
                     broker_artifact=artifact, runner=runner, request_transform=request_transform,
                     runtime_git_dir=runtime_git_dir)
    if artifact is None:
        return await _execute_staged(command, **arguments)
    lock = _BROKER_LOCKS.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with lock:
        return await _execute_staged(command, **arguments)


async def _execute_staged(command: str, *, scope, helper: Path, timeout: int,
                         registry=None, host_parent=None, extra_read=(), on_receipt=None,
                         broker_artifact=None, runner=None, request_transform=None,
                         runtime_git_dir=None) -> NativeExecutionReceipt:
    registry = registry or TransactionRegistry()
    discard = getattr(scope, "publication_mode", "publish") == "discard"
    independent = getattr(scope, "independent_offline", False)
    if independent and (not discard or broker_artifact is not None or request_transform is not None or runner is not None):
        raise stage.StagingRejected("Independent work requires the offline disposable native runner")
    revision = policy_revision(scope)
    record = registry.begin(scope.workspace, task_id=scope.task_id, policy_revision=revision,
                            **({"independent_command": command} if independent else {}))
    identity = record["id"]
    from mewcode.sandbox.execution_progress import emit
    emit("preparing", transaction_id=identity)
    token = CURRENT_TRANSACTION.set(identity)
    cancel, plan, lease_id, native = None, None, None, None
    python_runtime, python_config_times = None, None
    holder = {"owner_id": scope.task_id, "run_id": identity, "conversation_id": scope.task_id}
    settled, invoked = False, False
    claim = ExitStack()
    try:
        claim.enter_context(registry.claim(identity))
        parent = staging_parent(scope.workspace, registry, configured=host_parent)
        plan = stage.prepare_workspace(scope, parent, policy_revision=revision)
        from mewcode.sandbox.transaction_diagnostics import command_fingerprint
        registry.update(identity, "preparing", transaction=str(plan.transaction), manifest_sha256=plan.manifest_sha256,
                        holder=holder, metadata_broker_required=broker_artifact is not None,
                        publication_mode="discard" if discard else "publish",
                        command_sha256=command_fingerprint(command.removesuffix("; exit $LASTEXITCODE")))
        from mewcode.sandbox.python_runtime import discover_python_runtime, merge_extra_read
        python_runtime = discover_python_runtime(command, staged_workspace=plan.workspace,
            source_workspace=plan.source, baseline=plan.baseline, scope=scope,
            forbidden_roots=(registry.root, plan.transaction.parent, plan.transaction))
        if python_runtime is not None:
            registry.update(identity, "preparing", python_runtime_dependency=python_runtime.receipt)
        if broker_artifact is not None:
            from mewcode.sandbox.metadata_broker import transaction_scope
            executable, sha256 = broker_artifact
            claim.enter_context(transaction_scope(plan.source, plan.transaction,
                                                  executable=executable, sha256=sha256,
                                                  reserve_seconds=timeout + 480))
        if not discard:
            inventory, metadata_sha = metadata_inventory(plan)
            registry.update(identity, "preparing", metadata_sha256=metadata_sha)
        runtime = plan.transaction / "runtime"
        for part in ("temp", "profile/AppData/Local", "profile/AppData/Roaming"):
            (runtime / part).mkdir(parents=True, exist_ok=True)
        cancel = lpac.HostCancelEvent()
        derived_read = python_runtime.extra_read if python_runtime is not None else ()
        request = lpac.build_runtime_request(command, MappedScope(scope, plan.workspace), runtime,
            timeout=timeout, cancel_name=cancel.name,
            git_dir=runtime_git_dir if runtime_git_dir is not None else helper.parent / "git",
            extra_read=merge_extra_read(extra_read, derived_read),
            python_executable=python_runtime.executable if python_runtime is not None else None)
        if python_runtime is not None:
            python_runtime.validate_request(request)
        readonly, containers = protections(plan)
        envelope = {"schema": PROTOCOL, "request": request, "original_workspace": str(plan.source),
                    "readonly_paths": readonly, "pinned_containers": containers,
                    "request_id": request["request_id"], "timeout_ms": request["timeout_ms"]}
        cleanup_directory = plan.transaction / "cleanup"
        cleanup_directory.mkdir()
        envelope["cleanup_receipt_path"] = str(cleanup_directory / "receipt.json")
        if request_transform is not None:
            request_transform(envelope)
        # The outer protections/paths can exceed the limit even when the
        # nested request fits. A prelaunch rejection must not fence this
        # workspace forever as if a child had unknown cleanup.
        encoded_request = lpac.encode_request(envelope)
        # Host-private recovery input: preserve the very bytes bound by the
        # native cleanup proof, outside every child path grant.
        with (cleanup_directory / "request.bin").open("xb") as stream:
            stream.write(encoded_request)
            stream.flush()
            os.fsync(stream.fileno())
        if python_runtime is not None:
            python_runtime.validate_before_launch()
            python_config_times = python_runtime.prepare_execution_config()
        registry.update(identity, "executing", request_id=request["request_id"],
                        request_sha256=hashlib.sha256(encoded_request).hexdigest())

        def observed(receipt):
            nonlocal native
            native = receipt
            # Retain lifecycle facts only, not output, scripts or credentials.
            registry.update(identity, "native_done" if receipt.cleanup_confirmed is True else "cleanup_unknown",
                native_receipt={**{k: getattr(receipt, k) for k in
                                ("status", "exit_code", "backend", "cleanup_confirmed", "output_truncated")},
                                "command_started": receipt.execution_started, "error_code": receipt.error_code})
            if on_receipt is not None and (receipt.status != "exited" or receipt.cleanup_confirmed is not True):
                on_receipt(receipt)

        invoked = True
        native = await (runner(envelope, cancel, observed) if runner is not None else
                        lpac.invoke_helper(helper, envelope, cancel, on_receipt=observed))
        if python_runtime is not None and native.cleanup_confirmed is True:
            python_runtime.restore_execution_config(python_config_times)
        if native.is_error or native.cleanup_confirmed is not True or native.output_truncated:
            settled = native.cleanup_confirmed is True
            registry.update(identity, "rejected" if settled else "cleanup_unknown")
            result = replace(native, transaction_id=identity,
                             publication_state="rejected" if settled else "not_published")
            if not result.is_error:
                result = replace(result, status="denied", detail="Native candidate is incomplete; no publication")
            return result
        if discard:
            verified = stage.verify_workspace(plan, scope, native, policy_revision=policy_revision(scope))
            registry.update(identity, "no_changes", staged_digest=verified.staged_digest,
                            discarded_candidate_changes=len(verified.changes))
            settled = True
            return replace(native, transaction_id=identity, publication_state="discarded", command_started=True)

        def verify_metadata(verified):
            if broker_artifact is not None:
                from mewcode.sandbox.metadata_changes import classify_metadata_changes
                from mewcode.sandbox.workspace_metadata import capture_metadata
                verified = classify_metadata_changes(plan, scope, inventory, verified, capture=capture_metadata)
            metadata_preflight(plan, inventory, verified.changes)
            return verified
        verified = verify_metadata(stage.verify_workspace(plan, scope, native, policy_revision=policy_revision(scope)))
        registry.update(identity, "verified", staged_digest=verified.staged_digest)
        if not verified.changes:
            registry.update(identity, "no_changes")
            settled = True
            return replace(native, transaction_id=identity, publication_state="no_changes")
        scopes = lease_scopes(plan, verified.changes)
        if broker_artifact is not None:
            from mewcode.sandbox.metadata_broker import CURRENT
            CURRENT.get().require_remaining(150)
        leases = _leases(registry, scope)
        acquired = leases.acquire(**holder, **scopes, ttl_seconds=600)
        if not acquired["acquired"]:
            raise stage.StagingRejected("Publication targets have another write owner")
        lease_id = acquired["lease"]["lease_id"]
        registry.update(identity, "publishing", lease_id=lease_id, lease_scopes=scopes)
        # Synchronous bounded publication cannot be left running in a background
        # thread after cancellation. No awaits occur between verify and publish.
        deadline = time.monotonic() + 120
        published = publication.publish_workspace(plan, scope, native, leases=leases, lease_id=lease_id,
                    identity=holder, policy_revision=lambda: policy_revision(scope),
                    metadata_verifier=verify_metadata,
                    candidate_inventory=inventory["candidates"],
                    cancelled=lambda: time.monotonic() >= deadline)
        registry.update(identity, "published", publication=asdict(published))
        settled = True
        return replace(native, transaction_id=identity, publication_state="published")
    except asyncio.CancelledError:
        if native is not None and native.cleanup_confirmed is True:
            registry.update(identity, "rejected", reason="cancelled_before_publication")
            settled = True
        else:
            registry.update(identity, "cleanup_unknown", reason="cancelled_without_cleanup_receipt")
        raise
    except (OSError, ValueError, RuntimeError) as error:
        mutated = plan is not None and (plan.transaction / "publication/plan.json").exists()
        unknown = invoked and (native is None or native.cleanup_confirmed is not True)
        settled = not mutated and not unknown
        state = "cleanup_unknown" if unknown else "recovery_required" if mutated else "rejected"
        registry.update(identity, state, reason=type(error).__name__)
        return NativeExecutionReceipt("denied", detail=str(error), backend=lpac.BACKEND,
            stdout=native.stdout if native else "", stderr=native.stderr if native else "",
            exit_code=native.exit_code if native else None,
            command_started=native.execution_started if native else None if invoked else False,
            error_code=getattr(error, "error_code", "native_result_rejected" if invoked else "native_preparation_failed"),
            cleanup_confirmed=False if unknown else native.cleanup_confirmed if native else None,
            transaction_id=identity, publication_state=state)
    finally:
        try:
            if settled and lease_id:
                _leases(registry, scope).release(lease_id, **holder)
        finally:
            if cancel:
                cancel.close()
            claim.close()
            CURRENT_TRANSACTION.reset(token)
            if on_receipt is not None and native is not None and native.cleanup_confirmed is not True:
                on_receipt(replace(native, transaction_id=identity))


def reconcile(scope, registry: TransactionRegistry | None = None, *, confirmation=None,
              allow_publication_recovery=True) -> list[dict]:
    """Discard proven unpublished work; restore authority only for source recovery.

    Never rerun the command or auto-publish a candidate after interruption.
    Unknown native cleanup requires separate host cleanup evidence.
    """
    registry = registry or TransactionRegistry()
    outcomes = []
    records = registry.pending(scope.workspace)
    if confirmation is not None:
        if not isinstance(confirmation, dict):
            raise stage.StagingRejected("Invalid host recovery confirmation")
        records = [r for r in records if r["id"] == confirmation.get("transaction_id")]
        if not records:
            raise stage.StagingRejected("Confirmed recovery transaction is no longer pending")
    for record in records:
        identity = record["id"]
        token = CURRENT_TRANSACTION.set(identity)
        lease_id = None
        claim = ExitStack()
        try:
            claim.enter_context(registry.claim(identity))
            # Re-read after the lock: another process may have just settled it.
            records = [r for r in registry.pending(scope.workspace) if r["id"] == identity]
            if not records:
                continue
            record = records[0]
            handoff = None
            if confirmation is not None:
                from mewcode.sandbox.recovery_handoff import confirm_locked, record_confirmation
                handoff = confirm_locked(scope, record, confirmation)
                receipt_path = record_confirmation(registry, handoff)
                registry.update(identity, record["state"], last_recovery_handoff=receipt_path)
            if record["state"] == "preparing":
                # The executing record is durably written before spawning.
                # With the exclusive claim held this proves no child started.
                registry.update(identity, "rejected", reason="interrupted_before_native_launch")
                outcomes.append({"id": identity, "state": "rejected"})
                continue
            native = record.get("native_receipt", {})
            if native.get("cleanup_confirmed") is not True:
                from mewcode.sandbox.cleanup_evidence import read_cleanup_evidence
                if not record.get("request_sha256") or not record.get("transaction"):
                    raise stage.StagingRejected("Native cleanup remains unconfirmed; no bound host evidence")
                proof_path = Path(record["transaction"]) / "cleanup/receipt.json"
                proof = read_cleanup_evidence(proof_path, request_id=record["request_id"],
                                               request_sha256=record["request_sha256"])
                # Preserve original execution/transport outcome. Cleanup proof
                # does not authorize publishing or invent command success.
                native = {**native, "cleanup_confirmed": True}
                record = registry.update(identity, record["state"], native_receipt=native,
                                         cleanup_evidence=proof)
            plan = publication.restore_staged_plan(Path(record["transaction"]), source=scope.workspace,
                                                   manifest_sha256=record["manifest_sha256"])
            if not (plan.transaction / "publication/plan.json").exists():
                if record.get("lease_id"):
                    _leases(registry, scope).release(record["lease_id"], **record["holder"])
                registry.update(identity, "rejected", reason="interrupted_unpublished_candidate")
                outcomes.append({"id": identity, "state": "rejected"})
                continue
            if not allow_publication_recovery:
                outcomes.append({"id": identity, "state": "recovery_required",
                    "reason": "Interrupted publication needs its original metadata recovery capability"})
                continue
            if handoff is None and (record["task_id"] != scope.task_id
                                    or record["policy_revision"] != policy_revision(scope)):
                raise stage.StagingRejected("Restore the original current task authority before recovery")
            from mewcode.sandbox.metadata_broker import installed_artifact, transaction_scope
            if record.get("metadata_broker_required") is True:
                artifact = installed_artifact(lpac.find_runtime_helper(require_protocol=PROTOCOL))
                if artifact is None:
                    raise stage.StagingRejected("Recovery requires the original metadata preservation capability")
                claim.enter_context(transaction_scope(plan.source, plan.transaction,
                                                      executable=artifact[0], sha256=artifact[1]))
            leases = _leases(registry, scope)
            stored = leases.snapshot()["leases"]
            if any(item["lease_id"] == record["lease_id"] for item in stored):
                acquired = leases.renew_owned_for_recovery(record["lease_id"], **record["holder"], ttl_seconds=600)
                lease_id = acquired["lease_id"]
            else:
                # Another workspace may have purged an expired lease. Reacquire
                # exactly the host-recorded entries, never widen them to parents.
                acquired = leases.acquire(**record["holder"], **record["lease_scopes"], ttl_seconds=600)
                if not acquired["acquired"]:
                    raise stage.StagingRejected("Recovery target belongs to another writer")
                lease_id = acquired["lease"]["lease_id"]
                registry.update(identity, "recovery_required", previous_lease_id=record["lease_id"], lease_id=lease_id)
            result = publication.recover_publication(plan, scope, leases=leases, lease_id=lease_id,
                        identity=record["holder"], policy_revision=lambda: policy_revision(scope),
                        recovery_confirmation=handoff)
            if result.state not in {"published", "rolled_back"}:
                raise stage.StagingRejected("Recovery conflicts require reconciliation")
            registry.update(identity, result.state, publication=asdict(result))
            leases.release(lease_id, **record["holder"])
            outcomes.append({"id": identity, "state": result.state})
        except NativeTransactionBusy as error:
            # Busy is an observation, not a durable recovery transition. Never
            # read/repair a candidate while another executor/reconciler owns it.
            outcomes.append({"id": identity, "state": "busy", "reason": str(error)})
        except (OSError, ValueError, RuntimeError) as error:
            outcomes.append({"id": identity, "state": "recovery_required", "reason": str(error)})
        finally:
            claim.close()
            CURRENT_TRANSACTION.reset(token)
    return outcomes
