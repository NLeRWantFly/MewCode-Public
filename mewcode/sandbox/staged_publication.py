"""Host-only publication/recovery of verified native workspace candidates.

The gated native lifecycle calls this module with a live host scope,
positive lease ownership and the host's current policy fingerprint.
Old files are retained privately; every rename is no-replace. A crash or
conflict leaves a recovery plan, never a false success or an in-place partial
file. Multi-file publication is recoverable, NOT globally atomic to readers.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Callable

from mewcode.orchestration.write_lease import WriteLeaseStore
from mewcode.sandbox import staged_workspace as stage
from mewcode.sandbox.lpac_runtime import NativeScope, checked_path
from mewcode.sandbox.publication_io import FrozenSecurity, PinnedPath
from mewcode.sandbox.receipt import NativeExecutionReceipt

SCHEMA = "mewcode-staged-publication-v2"
MAX_JOURNAL_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class PublicationReceipt:
    state: str
    journal: str
    paths: tuple[str, ...]
    conflicts: tuple[str, ...] = ()


def _event(root: Path, state: str, **fields):
    # Prefix newline keeps a post-crash partial line separate. Events are
    # diagnostics, never the authority for target identity or rollback.
    with (root / "events.jsonl").open("ab") as stream:
        stream.write(b"\n" + stage._encoded({"state": state, **fields}) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _load(path: Path):
    checked_path(path, file=True)
    with path.open("rb") as stream:
        raw = stream.read(MAX_JOURNAL_BYTES + 1)
    if len(raw) > MAX_JOURNAL_BYTES:
        raise stage.StagingRejected("Publication journal exceeds host bound")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise stage.StagingRejected("Duplicate publication field")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique), raw


def restore_staged_plan(transaction: Path, *, source: Path, manifest_sha256: str) -> stage.StagedPlan:
    """Reload with an expected digest/source from a trusted host task record.

    Never use a digest supplied by the worker or read from the same manifest
    as proof of ownership. Restoration itself does not authorize publication.
    """
    checked_path(transaction)
    data, raw = _load(transaction / "manifest.json")
    if stage._hash(raw) != stage._revision(manifest_sha256) or data.get("schema") != stage.SCHEMA:
        raise stage.StagingRejected("Saved staged manifest does not match the host record")
    if data.get("source") != str(source) or data.get("workspace") != str(transaction / "workspace"):
        raise stage.StagingRejected("Saved staged paths do not match the host record")
    baseline = tuple(stage.Entry(**entry) for entry in data["baseline"])
    for entry in baseline:
        _relative(entry.relative, allow_root=True)
    if (not baseline or len(baseline) > stage.MAX_ENTRIES
            or len({e.relative for e in baseline}) != len(baseline)):
        raise stage.StagingRejected("Invalid saved baseline")
    return stage.StagedPlan(transaction, source, transaction / "workspace", data["policy_revision"],
                            baseline, manifest_sha256, data["max_entries"], data["max_bytes"])


def _relative(value, *, allow_root=False):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or Path(value).is_absolute() or any(part in {"", "..", "."} for part in value.split("/"))):
        if not (allow_root and value == "."):
            raise stage.StagingRejected("Invalid publication relative path")
    return value


def _pins(stack: ExitStack, paths):
    directories = set()
    for path in paths:
        directories.update(path.parents)
        directories.add(path)
    handles = {}
    for path in sorted(directories, key=lambda p: (len(p.parts), str(p).casefold())):
        handles[path] = stack.enter_context(PinnedPath(path, directory=True))
    return handles


def _recheck(plan, scope, policy_revision, lease_check, cancelled, targets, *, recovery_revision=None):
    lease_check()
    if cancelled():
        raise stage.StagingRejected("Publication cancelled; recovery is required if mutations began")
    if (not scope.current() or scope.workspace != plan.source
            or stage._revision(policy_revision()) != (recovery_revision or plan.policy_revision)):
        raise stage.StagingRejected("Publication task or policy changed")
    if any(not scope.allows(plan.source / relative, write=True) for relative in targets):
        raise stage.StagingRejected("Publication target is no longer writable")


def _names(root: Path, scope: NativeScope):
    """Namespace check without reopening content pinned with DELETE access."""
    names, pending = set(), [root]
    while pending:
        path = pending.pop()
        if not scope.allows(path, write=False):
            continue
        checked_path(path)
        names.add(path.relative_to(root).as_posix())
        if len(names) + len(pending) > stage.MAX_ENTRIES:
            raise stage.StagingRejected("Publication namespace exceeds bound")
        if path.is_dir():
            for child in path.iterdir():
                pending.append(child)
                if len(names) + len(pending) > stage.MAX_ENTRIES:
                    raise stage.StagingRejected("Publication namespace exceeds bound")
    return names


def publish_workspace(plan: stage.StagedPlan, scope: NativeScope, receipt: NativeExecutionReceipt, *,
                      leases: WriteLeaseStore, lease_id: str, identity: dict[str, str],
                      policy_revision: Callable[[], str], cancelled: Callable[[], bool] = lambda: False,
                      metadata_verifier=None, candidate_inventory=None
                      ) -> PublicationReceipt:
    """Publish with host ownership; failures retain old versions and a journal.

    Directories are removed only by a kernel empty-directory check, after
    children have been retained privately. Rollback recreates those directory
    objects with recorded identities/permissions; it never moves an unknown
    external subtree or silently retains a requested deletion.
    """
    verified = stage.verify_workspace(plan, scope, receipt, policy_revision=policy_revision())
    if metadata_verifier is not None:
        verified = metadata_verifier(verified)
    before = {entry.relative: entry for entry in plan.baseline}
    targets = tuple(_relative(c.relative) for c in verified.changes)
    if not targets:
        raise stage.StagingRejected("No candidate changes to publish")
    journal = plan.transaction / "publication"
    with leases.hold_owned_paths(lease_id, [plan.source / name for name in targets], **identity) as check_lease:
        check = lambda: _recheck(plan, scope, policy_revision, check_lease, cancelled, targets)
        check()
        journal.mkdir()  # Never reuse a prior publication; use recovery instead.
        (journal / "old").mkdir()
        (journal / "incoming").mkdir()
        stage._write_new(journal / "lock", b"host publication")
        try:
            with ExitStack() as stack:
                stack.enter_context(PinnedPath(journal / "lock", exclusive=True))
                dirs = _pins(stack, [plan.source, plan.workspace, journal / "old", journal / "incoming"])
                if dirs[plan.source].file_id[0] != dirs[journal].file_id[0]:
                    raise stage.StagingRejected("Publication storage must be on the source volume")
                old_handles, incoming, entries, desired_security = {}, {}, [], {}
                # Pin every preexisting readable source object. Targets get
                # DELETE on their existing handle; other files are read-only.
                for entry in sorted(plan.baseline, key=lambda e: (len(Path(e.relative).parts), e.relative)):
                    path = plan.source / entry.relative
                    if entry.kind == "directory":
                        handle = dirs.get(path)
                        if handle is None:
                            handle = stack.enter_context(PinnedPath(path, directory=True))
                            dirs[path] = handle
                    else:
                        handle = stack.enter_context(PinnedPath(path, movable=entry.relative in targets))
                    if (handle.file_id[1] != entry.inode & ((1 << 64) - 1)
                            or handle.digest(plan.max_bytes) != entry.sha256):
                        raise stage.StagingRejected("Source identity or content drifted before publication")
                    old_handles[entry.relative] = handle
                    if entry.relative in targets:
                        handle.require_plain_metadata(allow_readonly=metadata_verifier is not None and entry.kind == "file")
                if _names(plan.source, scope) != set(before):
                    raise stage.StagingRejected("Source namespace drifted before publication")
                # Freeze private payloads before making ANY source mutation.
                for index, change in enumerate(verified.changes):
                    relative = change.relative
                    entry = {**asdict(change), "slot": str(index),
                             "kind": before[relative].kind if relative in before else "file",
                             "old_id": list(old_handles[relative].file_id) if relative in before else None,
                             "new_id": None}
                    if entry["kind"] == "file" and relative in old_handles:
                        entry["original_basic"] = old_handles[relative].basic_metadata()
                    if change.operation == "delete" and entry["kind"] == "directory":
                        # A no-replace rename cannot atomically assert an empty
                        # directory. Keep an EMPTY recovery object instead;
                        # never move a possibly growing external subtree.
                        original = old_handles[relative]
                        replacement = journal / "old" / str(index)
                        replacement.mkdir()
                        handle = stack.enter_context(PinnedPath(replacement, movable=True,
                                                               directory=True, security_write=True, metadata_write=True))
                        entry["original_basic"] = original.basic_metadata()
                        handle.set_basic_metadata(entry["original_basic"])
                        entry["original_security"] = original.security_descriptor().hex()
                        if handle.access_signature() != original.access_signature():
                            handle.copy_dacl_from(original)
                        handle.copy_sacl_from(original)
                        entry["restore_id"] = list(handle.file_id)
                    if change.operation != "delete":
                        source = checked_path(plan.workspace / relative)
                        with PinnedPath(source, directory=source.is_dir()) as candidate:
                            candidate.require_plain_metadata(allow_readonly=not source.is_dir()
                                and (relative not in before or metadata_verifier is not None))
                            candidate_basic = candidate.basic_metadata()
                        payload = journal / "incoming" / str(index)
                        if source.is_dir():
                            entry["kind"] = "directory"
                            payload.mkdir()
                        else:
                            digest, _ = stage._file_digest(source, plan.max_bytes, payload)
                            if digest != change.after_sha256:
                                raise stage.StagingRejected("Candidate changed after verification")
                        handle = stack.enter_context(PinnedPath(payload, movable=True, directory=entry["kind"] == "directory",
                                                               security_write=True, metadata_write=True))
                        if handle.digest(plan.max_bytes) != change.after_sha256:
                            raise stage.StagingRejected("Private publication payload changed")
                        if relative in old_handles:
                            original = old_handles[relative]
                            # Preserve the source creation time unless host
                            # metadata verification approved an explicit delta.
                            # Write time comes from the candidate, not the copy.
                            original_basic = original.basic_metadata()
                            original_candidate = (candidate_inventory or {}).get(relative, {})
                            if (metadata_verifier is None or candidate_basic["created"] == original_candidate.get("created")):
                                candidate_basic.update(created=original_basic["created"])
                            if metadata_verifier is None:
                                candidate_basic["attributes"] = original_basic["attributes"]
                            entry["copied_dacl"] = handle.access_signature() != original.access_signature()
                            if entry["copied_dacl"]:
                                handle.copy_dacl_from(original)
                            handle.copy_sacl_from(original)
                        else:
                            parent_name = Path(relative).parent.as_posix()
                            parent_security = desired_security.get(parent_name) or old_handles[parent_name]
                            expected = FrozenSecurity.inherited(parent_security, directory=entry["kind"] == "directory")
                            desired_security[relative] = expected
                            entry["published_security"] = expected.security_descriptor().hex()
                            entry["copied_dacl"] = handle.access_signature() != expected.access_signature()
                            if entry["copied_dacl"]:
                                handle.copy_dacl_from(expected)
                            handle.copy_sacl_from(expected)
                        handle.set_basic_metadata(candidate_basic)
                        entry["published_basic"] = {k: candidate_basic[k] for k in ("created", "written", "attributes")}
                        incoming[relative] = handle
                        entry["new_id"] = list(handle.file_id)
                    entries.append(entry)
                document = {"schema": SCHEMA, "manifest_sha256": plan.manifest_sha256,
                            "policy_revision": plan.policy_revision, "source": str(plan.source),
                            "staged_digest": verified.staged_digest,
                            "holder": identity, "lease_id": lease_id, "entries": entries}
                from mewcode.sandbox.metadata_broker import CURRENT
                document["metadata_broker_required"] = CURRENT.get() is not None
                document_bytes = stage._encoded(document)
                if len(document_bytes) > MAX_JOURNAL_BYTES:
                    raise stage.StagingRejected("Publication recovery plan exceeds host bound")
                stage._write_new(journal / "plan.json", document_bytes)
                _event(journal, "prepared")
                check()
                # Directories first, Git object payloads before refs/index.
                def order(entry):
                    rel = entry["relative"].casefold()
                    if entry["kind"] == "directory" and entry["operation"] == "delete":
                        return 3, -len(Path(rel).parts), rel
                    rank = 0 if entry["kind"] == "directory" else 2 if rel.startswith(".git/refs/") or rel in {".git/head", ".git/index"} else 1
                    return rank, len(Path(rel).parts), rel
                for entry in sorted(entries, key=order):
                    check()
                    relative = entry["relative"]
                    _event(journal, "applying", relative=relative)
                    if entry["kind"] == "directory" and entry["operation"] == "delete":
                        old_handles[relative].prepare_directory_removal()
                        check()
                        old_handles[relative].remove_empty_directory()
                    elif relative in before:
                        old_handles[relative].rename_absent(journal / "old" / entry["slot"])
                    check()  # Revocation/cancel in the rename gap cannot publish.
                    if entry["operation"] != "delete":
                        incoming[relative].rename_absent(plan.source / relative)
                        # NTFS name tunneling may replace creation time during
                        # rename. Reapply the verified fields on our pinned new
                        # object, never on the displaced original.
                        published_basic = incoming[relative].basic_metadata()
                        published_basic.update(entry["published_basic"])
                        incoming[relative].set_basic_metadata(published_basic)
                        if relative not in before:
                            expected = desired_security[relative]
                            if entry.get("copied_dacl"):
                                incoming[relative].copy_dacl_from(expected, final=True)
                            if incoming[relative].access_signature() != expected.access_signature():
                                raise stage.StagingRejected("Published inheritance differs from target parent")
                            incoming[relative].copy_sacl_from(expected, final=True)
                        else:
                            if entry.get("copied_dacl"):
                                incoming[relative].copy_dacl_from(old_handles[relative], final=True)
                            if incoming[relative].access_signature() != old_handles[relative].access_signature():
                                raise stage.StagingRejected("Published permissions differ from original")
                            incoming[relative].copy_sacl_from(old_handles[relative], final=True)
                        if entry["kind"] == "directory":
                            incoming[relative].pin_renamed_directory()
                    _event(journal, "applied", relative=relative)
                check()
                expected_names = (set(before) - {c.relative for c in verified.changes if c.operation == "delete"}) | {
                    c.relative for c in verified.changes if c.operation != "delete"}
                if _names(plan.source, scope) != expected_names:
                    raise stage.StagingRejected("Concurrent namespace change requires reconciliation")
                for entry in entries:
                    if entry["operation"] != "delete" and incoming[entry["relative"]].digest(plan.max_bytes) != entry["after_sha256"]:
                        raise stage.StagingRejected("Published payload changed")
                    if entry["operation"] != "delete" and entry["kind"] == "file":
                        actual = incoming[entry["relative"]].basic_metadata()
                        if any(actual[k] != v for k, v in entry["published_basic"].items()):
                            raise stage.StagingRejected("Published basic metadata differs from verified candidate")
                _event(journal, "published")
                stage._write_new(journal / "published.json", stage._encoded({
                    "state": "published", "plan_sha256": stage._hash(stage._encoded(document))}))
                return PublicationReceipt("published", str(journal), targets)
        except BaseException:
            _event(journal, "recovery_required")
            raise


def recover_publication(plan: stage.StagedPlan, scope: NativeScope, *, leases: WriteLeaseStore,
                        lease_id: str, identity: dict[str, str], policy_revision: Callable[[], str],
                        rollback_published: bool = False, recovery_confirmation=None
                        ) -> PublicationReceipt:
    """Conservatively roll back a host journal, including after process death.

    External edits/identity changes are conflicts, never overwritten. A live
    matching identity and current authority are required even for rollback.
    The caller may reacquire an expired lease; journal ownership cannot change.
    """
    journal = checked_path(plan.transaction / "publication")
    data, raw = _load(journal / "plan.json")
    recovery_revision = None
    if recovery_confirmation is not None:
        grant = recovery_confirmation
        if (grant.get("schema") != "mewcode-recovery-handoff-v1"
                or grant.get("journal_sha256") != stage._hash(raw)
                or grant.get("manifest_sha256") != plan.manifest_sha256
                or grant.get("workspace") != str(plan.source)
                or grant.get("original_policy_revision") != plan.policy_revision
                or grant.get("current_task_id") != scope.task_id
                or grant.get("action") != "verify_committed_or_rollback_unfinished"
                or rollback_published):
            raise stage.StagingRejected("Recovery confirmation does not match the immutable journal")
        recovery_revision = stage._revision(grant["current_policy_revision"])
    from mewcode.sandbox.metadata_broker import CURRENT
    if data.get("metadata_broker_required") is True and CURRENT.get() is None:
        raise stage.StagingRejected("Recovery requires the original metadata preservation capability")
    if (data.get("schema") not in {SCHEMA, "mewcode-staged-publication-v1"}
            or data.get("manifest_sha256") != plan.manifest_sha256
            or data.get("source") != str(plan.source) or data.get("holder") != identity
            or data.get("policy_revision") != plan.policy_revision):
        raise stage.StagingRejected("Recovery journal does not match the host task")
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries or len(entries) > plan.max_entries:
        raise stage.StagingRejected("Invalid recovery entries")
    targets = tuple(_relative(e["relative"]) for e in entries)
    baseline = {entry.relative: entry for entry in plan.baseline}
    for i, entry in enumerate(entries):
        if (entry.get("slot") != str(i) or entry.get("operation") not in {"create", "modify", "delete"}
                or entry.get("kind") not in {"file", "directory"} or len(set(targets)) != len(targets)):
            raise stage.StagingRejected("Invalid recovery operation")
        old = baseline.get(entry["relative"])
        if ((entry["operation"] == "create") != (old is None)
                or old is not None and (entry["before_sha256"] != old.sha256 or not old.writable)
                or old is not None and entry["kind"] != old.kind
                or entry["kind"] == "directory" and entry["operation"] == "modify"):
            raise stage.StagingRejected("Recovery operation differs from frozen baseline")
        if entry["kind"] == "directory" and entry["operation"] == "delete":
            if data["schema"] != SCHEMA or entry.get("restore_id") is None:
                raise stage.StagingRejected("Missing directory recovery identity")
            FrozenSecurity(bytes.fromhex(entry.get("original_security", ""))).security_descriptor()
            if not isinstance(entry.get("original_basic"), dict):
                raise stage.StagingRejected("Missing directory recovery metadata")
        for field in ("old_id", "new_id", "restore_id"):
            value = entry.get(field)
            if value is not None and (not isinstance(value, list) or len(value) != 2
                                     or any(type(part) is not int or part < 0 for part in value)):
                raise stage.StagingRejected("Invalid recovery object identity")
        for field in ("before_sha256", "after_sha256"):
            value = entry.get(field)
            if value is not None and value != "":
                stage._revision(value)
    conflicts = []
    with leases.hold_owned_paths(lease_id, [plan.source / name for name in targets], **identity) as check_lease:
        check = lambda: _recheck(plan, scope, policy_revision, check_lease, lambda: False, targets,
                                 recovery_revision=recovery_revision)
        check()
        with ExitStack() as stack:
            stack.enter_context(PinnedPath(journal / "lock", exclusive=True))
            _pins(stack, [plan.source, journal / "old", journal / "incoming"])
            committed = journal / "published.json"
            rolled_back = journal / "rolled-back.json"
            if rolled_back.exists():
                marker, _ = _load(rolled_back)
                if marker != {"state": "rolled_back", "plan_sha256": stage._hash(raw)}:
                    raise stage.StagingRejected("Recovery completion marker changed")
                # Verify rather than replay a completed rollback. An external
                # edit after rollback remains untouched on every restart.
                for entry in entries:
                    target = plan.source / entry["relative"]
                    try:
                        if entry["operation"] == "create":
                            if os.path.lexists(target):
                                conflicts.append(entry["relative"])
                        else:
                            with PinnedPath(target, directory=entry["kind"] == "directory") as current:
                                if (list(current.file_id) not in [entry["old_id"], entry.get("restore_id")]
                                        or current.digest(plan.max_bytes) != entry["before_sha256"]):
                                    conflicts.append(entry["relative"])
                                elif entry["kind"] == "file" and entry.get("original_basic"):
                                    actual = current.basic_metadata()
                                    if any(actual[k] != entry["original_basic"][k] for k in ("created", "written", "attributes")):
                                        conflicts.append(entry["relative"])
                    except (OSError, stage.NativeBoundaryUnavailable):
                        conflicts.append(entry["relative"])
                return PublicationReceipt("recovery_conflict" if conflicts else "rolled_back", str(journal), targets, tuple(conflicts))
            if committed.exists() and not rollback_published:
                marker, _ = _load(committed)
                if marker != {"state": "published", "plan_sha256": stage._hash(raw)}:
                    raise stage.StagingRejected("Publication completion marker changed")
                # Restart must not silently undo a successfully delivered
                # result. Report drift separately; never reapply or overwrite.
                for entry in entries:
                    target = plan.source / entry["relative"]
                    try:
                        if entry["operation"] == "delete":
                            if os.path.lexists(target):
                                conflicts.append(entry["relative"])
                        else:
                            with PinnedPath(target, directory=entry["kind"] == "directory") as current:
                                if (list(current.file_id) != entry["new_id"]
                                        or current.digest(plan.max_bytes) != entry["after_sha256"]):
                                    conflicts.append(entry["relative"])
                                elif entry["kind"] == "file" and entry.get("published_basic"):
                                    actual = current.basic_metadata()
                                    if any(actual[k] != value for k, value in entry["published_basic"].items()):
                                        conflicts.append(entry["relative"])
                    except (OSError, stage.NativeBoundaryUnavailable):
                        conflicts.append(entry["relative"])
                return PublicationReceipt("published_drift" if conflicts else "published", str(journal), targets, tuple(conflicts))
            # Restore removed directories shallow-first, then undo file
            # writes, then remove created directories deepest-first.
            def rollback_order(entry):
                depth = len(Path(entry["relative"]).parts)
                if entry["kind"] == "directory" and entry["operation"] == "delete":
                    return 0, depth
                return 1, -depth
            by_relative = {entry["relative"]: entry for entry in entries}
            for entry in sorted(entries, key=rollback_order):
                check()
                relative, slot = entry["relative"], entry["slot"]
                target, backup, payload = plan.source / relative, journal / "old" / slot, journal / "incoming" / slot
                try:
                    if any(parent.as_posix() in conflicts for parent in Path(relative).parents):
                        raise stage.StagingRejected("Recovery parent has an unresolved conflict")
                    if entry["operation"] == "create" and not os.path.lexists(target):
                        continue  # Already removed; its created parent may also be gone.
                    with ExitStack() as item_stack:
                        # Pin all existing parent components for this target.
                        parent_pins = _pins(item_stack, [target.parent])
                        # Do not populate an external replacement parent with
                        # our saved files, even if it is currently empty.
                        for parent, handle in parent_pins.items():
                            if not parent.is_relative_to(plan.source):
                                continue
                            rel = parent.relative_to(plan.source).as_posix()
                            changed_parent = by_relative.get(rel, {})
                            original_parent = baseline.get(rel)
                            allowed_ids = [changed_parent.get("restore_id"), changed_parent.get("new_id")]
                            if original_parent is not None:
                                allowed_ids.append([handle.file_id[0], original_parent.inode & ((1 << 64) - 1)])
                            if list(handle.file_id) not in allowed_ids:
                                raise stage.StagingRejected("External parent retained during recovery")
                        target_handle = None
                        old = None
                        if entry["kind"] == "directory" and entry["operation"] == "delete":
                            security = FrozenSecurity(bytes.fromhex(entry["original_security"]))
                            if os.path.lexists(target):
                                restored = item_stack.enter_context(PinnedPath(target, directory=True, security_write=True))
                                observed = list(restored.file_id)
                                if observed == entry["old_id"]:
                                    continue  # Directory removal never applied.
                                if observed != entry["restore_id"] or os.path.lexists(backup):
                                    raise stage.StagingRejected("External directory retained during recovery")
                                if CURRENT.get() is not None:
                                    CURRENT.get().register_recovery(restored, journal)
                            else:
                                restored = item_stack.enter_context(PinnedPath(backup, movable=True,
                                                                             directory=True, security_write=True))
                                if list(restored.file_id) != entry["restore_id"] or any(backup.iterdir()):
                                    raise stage.StagingRejected("Directory recovery payload changed")
                                restored.require_plain_metadata()
                                # Register while it is still a host-private
                                # restoration object, before moving it back.
                                restored.copy_sacl_from(security)
                                check()
                                restored.rename_absent(target)
                            if restored.access_signature() != security.access_signature():
                                check()
                                restored.copy_dacl_from(security, final=True)
                            if restored.access_signature() != security.access_signature():
                                raise stage.StagingRejected("Restored directory permissions differ")
                            restored.copy_sacl_from(security, final=True)
                            _event(journal, "restored", relative=relative)
                            continue
                        if os.path.lexists(target):
                            target_handle = item_stack.enter_context(PinnedPath(target, movable=True, directory=entry["kind"] == "directory"))
                            observed = list(target_handle.file_id)
                            digest = target_handle.digest(plan.max_bytes)
                            if observed == entry["old_id"] and digest == entry["before_sha256"]:
                                if os.path.lexists(backup):
                                    raise stage.StagingRejected("Both original and backup exist")
                                if entry.get("original_basic"):
                                    actual = target_handle.basic_metadata()
                                    if any(actual[k] != entry["original_basic"][k] for k in ("created", "written", "attributes")):
                                        raise stage.StagingRejected("Restored original metadata requires reconciliation")
                                continue  # Never applied, or already restored.
                            if observed != entry["new_id"] or digest != entry["after_sha256"]:
                                raise stage.StagingRejected("External version retained during recovery")
                        if entry["operation"] != "create":
                            old = item_stack.enter_context(PinnedPath(backup, movable=True, metadata_write=True))
                            if list(old.file_id) != entry["old_id"] or old.digest(plan.max_bytes) != entry["before_sha256"]:
                                raise stage.StagingRejected("Recovery backup identity or content changed")
                            if entry.get("original_basic"):
                                actual = old.basic_metadata()
                                if any(actual[k] != entry["original_basic"][k] for k in ("created", "written", "attributes")):
                                    raise stage.StagingRejected("Recovery backup metadata changed")
                        # Validate backup before moving the current candidate.
                        check()
                        if target_handle is not None:
                            if entry["kind"] == "directory":
                                target_handle.remove_empty_directory()
                            else:
                                target_handle.rename_absent(payload)
                        if old is not None:
                            check()
                            original_basic = old.basic_metadata()
                            old.rename_absent(target)
                            # Undo NTFS name tunneling on the exact restored
                            # original, using metadata read from its pinned
                            # backup immediately before the rename.
                            old.set_basic_metadata(original_basic)
                            actual = old.basic_metadata()
                            if any(actual[k] != original_basic[k] for k in ("created", "written", "attributes")):
                                raise stage.StagingRejected("Restored basic metadata differs from original")
                        _event(journal, "restored", relative=relative)
                except (OSError, ValueError, stage.NativeBoundaryUnavailable) as error:
                    conflicts.append(relative)
                    _event(journal, "recovery_conflict", relative=relative, reason=type(error).__name__,
                           detail=str(error))
            # Child restoration updates directory times. Restore original
            # timestamps last, and only on our reconstructed directory IDs.
            # Never reset metadata on an external or never-deleted directory.
            for entry in sorted(entries, key=lambda e: -len(Path(e["relative"]).parts)):
                if entry["kind"] != "directory" or entry["operation"] != "delete":
                    continue
                relative = entry["relative"]
                if any(name == relative or name.startswith(relative + "/") for name in conflicts):
                    continue
                try:
                    check()
                    with ExitStack() as item_stack:
                        _pins(item_stack, [(plan.source / relative).parent])
                        current = item_stack.enter_context(PinnedPath(plan.source / relative, directory=True, metadata_write=True))
                        if list(current.file_id) == entry["restore_id"]:
                            current.set_basic_metadata(entry["original_basic"])
                        elif list(current.file_id) != entry["old_id"]:
                            raise stage.StagingRejected("Directory changed before metadata restoration")
                except (OSError, ValueError, stage.NativeBoundaryUnavailable) as error:
                    conflicts.append(relative)
                    _event(journal, "recovery_conflict", relative=relative, reason=type(error).__name__, detail=str(error))
            state = "recovery_conflict" if conflicts else "rolled_back"
            _event(journal, state)
            if not conflicts:
                stage._write_new(rolled_back, stage._encoded({"state": "rolled_back", "plan_sha256": stage._hash(raw)}))
            return PublicationReceipt(state, str(journal), targets, tuple(conflicts))
