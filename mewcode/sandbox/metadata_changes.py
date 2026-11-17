"""Classify narrowly supported metadata-only changes in a staged workspace.

This module only augments host-verified staged changes. It never treats worker
claims as authority and its result is still an unpublished verification.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Callable, Mapping

from mewcode.sandbox import staged_workspace as stage


_SNAPSHOT_KEYS = frozenset({
    "schema", "kind", "capture_consistent", "attributes", "created",
    "written", "security", "streams", "extended_attributes",
})
_SECURITY_KEYS = frozenset({"owner_group_dacl", "sacl"})
_OWNER_KEYS = frozenset({"state", "sddl"})
_SACL_KEYS = frozenset({"state", "present", "null", "sddl"})
_STREAMS_KEYS = frozenset({"state", "items"})
_EA_KEYS = frozenset({"state", "size"})
_STREAM_ITEM_KEYS = frozenset({"name", "size", "allocation_size"})
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


def _reject(message: str) -> None:
    raise stage.StagingRejected(message)


def _mapping(value: object, keys: frozenset[str], message: str) -> Mapping:
    if not isinstance(value, dict) or set(value) != keys:
        _reject(message)
    return value


def _validate_snapshot(snapshot: object, expected_kind: str) -> dict:
    """Reject malformed and future metadata fields until explicitly supported."""
    value = _mapping(snapshot, _SNAPSHOT_KEYS, "Candidate metadata has unknown or missing fields")
    from mewcode.sandbox.workspace_metadata import SCHEMA

    if (value["schema"] != SCHEMA or value["kind"] != expected_kind
            or value["capture_consistent"] is not True):
        _reject("Candidate metadata is inconsistent or has an unsupported kind")
    for field in ("attributes", "created", "written"):
        if type(value[field]) is not int or value[field] < 0:
            _reject("Candidate basic metadata is invalid")
    security = _mapping(value["security"], _SECURITY_KEYS,
                        "Candidate security metadata has unknown or missing fields")
    owner = _mapping(security["owner_group_dacl"], _OWNER_KEYS,
                     "Candidate owner or DACL metadata has unknown fields")
    if owner["state"] == "captured":
        if not isinstance(owner["sddl"], str):
            _reject("Candidate owner or DACL metadata is unavailable")
    elif owner["state"] == "unavailable" and owner["sddl"] is None:
        pass
    else:
        _reject("Candidate owner or DACL metadata is invalid")

    sacl = _mapping(security["sacl"], _SACL_KEYS,
                    "Candidate SACL metadata has unknown or missing fields")
    if sacl["state"] == "captured":
        if (type(sacl["present"]) is not bool
                or type(sacl["null"]) is not bool
                or (sacl["present"] is False and sacl["null"] is not False)
                or (sacl["null"] is True and sacl["present"] is not True)
                or (sacl["sddl"] is not None and not isinstance(sacl["sddl"], str))):
            _reject("Candidate SACL metadata is invalid")
    elif (sacl["state"] == "unavailable" and sacl["present"] is None
          and sacl["null"] is None and sacl["sddl"] is None):
        pass
    else:
        _reject("Candidate SACL metadata is invalid")

    streams = _mapping(value["streams"], _STREAMS_KEYS,
                       "Candidate stream metadata has unknown or missing fields")
    if streams["state"] == "captured":
        if not isinstance(streams["items"], list):
            _reject("Candidate stream inventory is invalid")
        for item in streams["items"]:
            item_value = _mapping(item, _STREAM_ITEM_KEYS,
                                  "Candidate stream record has unknown or missing fields")
            if (not isinstance(item_value["name"], str)
                    or type(item_value["size"]) is not int or item_value["size"] < 0
                    or type(item_value["allocation_size"]) is not int
                    or item_value["allocation_size"] < 0):
                _reject("Candidate stream record is invalid")
    elif streams["state"] == "unavailable" and streams["items"] == []:
        pass
    else:
        _reject("Candidate stream metadata is invalid")

    extended = _mapping(value["extended_attributes"], _EA_KEYS,
                        "Candidate extended-attribute metadata has unknown fields")
    if extended["state"] == "captured":
        if type(extended["size"]) is not int or extended["size"] < 0:
            _reject("Candidate extended-attribute metadata is invalid")
    elif extended["state"] == "unavailable" and extended["size"] is None:
        pass
    else:
        _reject("Candidate extended-attribute metadata is invalid")
    return value


def _fully_comparable(snapshot: dict) -> bool:
    security = snapshot["security"]
    return (
        security["owner_group_dacl"]["state"] == "captured"
        and security["sacl"]["state"] == "captured"
        and snapshot["streams"]["state"] == "captured"
        and snapshot["extended_attributes"]["state"] == "captured"
    )


def _streams_unchanged(before: dict, current: dict, entry: stage.Entry,
                       change: stage.Change | None, candidate_size: int) -> bool:
    if before == current:
        return True
    # For a verified file-content modification, the unnamed data stream's
    # length and allocation can follow the new payload. Named streams and
    # same-length allocation changes remain unsupported metadata changes.
    if entry.kind != "file" or change is None or change.operation != "modify":
        return False
    left, right = before.get("items"), current.get("items")
    default_name = "::$DATA"
    if (not isinstance(left, list) or not isinstance(right, list)
            or len(left) != 1 or len(right) != 1
            or left[0].get("name") != default_name or right[0].get("name") != default_name
            or left[0].get("size") != entry.size
            or right[0].get("size") != candidate_size):
        return False
    return True


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_regular_file(path: Path, baseline: stage.Entry, plan: stage.StagedPlan) -> None:
    """Rehash a metadata-only candidate without following links."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise stage.StagingRejected("Metadata-only candidate cannot be inspected") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        _reject("Metadata-only candidate must be an unlinked regular file")
    digest, size = stage._file_digest(path, plan.max_bytes)
    try:
        after = path.lstat()
    except OSError as exc:
        raise stage.StagingRejected("Metadata-only candidate changed while hashing") from exc
    if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
            or _signature(before) != _signature(after)):
        _reject("Metadata-only candidate changed while hashing")
    if size != baseline.size or digest != baseline.sha256:
        _reject("Unverified candidate content changed; host verification is required")


def classify_metadata_changes(plan: stage.StagedPlan, scope, inventory: Mapping,
                              verified: stage.VerifiedChanges, *, capture: Callable[[Path], dict]
                              ) -> stage.VerifiedChanges:
    """Merge safe metadata-only file changes into host-verified changes.

    Only basic file attributes and creation/write timestamps may add a change.
    Security, streams, EAs, directory metadata-only changes and unknown fields
    are rejected. The returned digest commits to the original staged digest
    and every supported metadata increment.
    """
    if not isinstance(inventory, Mapping):
        _reject("Invalid candidate metadata inventory")
    sources, candidates = inventory.get("sources"), inventory.get("candidates")
    if not isinstance(sources, Mapping) or not isinstance(candidates, Mapping):
        _reject("Invalid candidate metadata inventory")

    baseline: dict[str, stage.Entry] = {}
    for entry in plan.baseline:
        if not isinstance(entry, stage.Entry) or entry.relative in baseline:
            _reject("Invalid frozen workspace baseline")
        baseline[entry.relative] = entry
    if set(sources) != set(baseline) or set(candidates) != set(baseline):
        _reject("Candidate metadata inventory does not match the frozen baseline")
    if (not isinstance(verified, stage.VerifiedChanges)
            or verified.state != "verified_unpublished"
            or not isinstance(verified.staged_digest, str)
            or not _DIGEST_RE.fullmatch(verified.staged_digest)):
        _reject("Invalid host-verified staged result")

    changes: dict[str, stage.Change] = {}
    for change in verified.changes:
        if not isinstance(change, stage.Change):
            _reject("Invalid host-verified change set")
        relative_path = Path(change.relative) if isinstance(change.relative, str) else None
        if (relative_path is None or not change.relative or relative_path.is_absolute()
                or relative_path.drive or ".." in relative_path.parts
                or change.relative in changes
                or change.operation not in {"create", "delete", "modify"}):
            _reject("Invalid host-verified change set")
        changes[change.relative] = change

    # Keep the normalization deliberately identical to metadata preflight:
    # only disposable-copy AI/ID DACL markings and exact unprotected zero-ACE
    # audit ACL forms may be materialization noise; originals stay exact.
    from mewcode.sandbox.staged_execution import _candidate_security
    from mewcode.sandbox.workspace_metadata import _SUPPORTED_ATTRIBUTES

    deltas: list[dict] = []
    for relative in sorted(baseline):
        entry = baseline[relative]
        path = plan.workspace / relative
        # Validate source inventory shape too: an unrecognized source field
        # must not become invisible to later metadata-drift checks.
        _validate_snapshot(sources[relative], entry.kind)
        change = changes.get(relative)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if change is not None and change.operation == "delete":
                continue
            _reject("An original staged candidate path is missing")
        except OSError as exc:
            raise stage.StagingRejected("Candidate path cannot be inspected") from exc

        if change is not None and change.operation == "delete":
            _reject("Deleted candidate path still exists")
        if change is not None and change.operation == "create":
            _reject("A new candidate cannot replace an original path")
        if entry.kind == "directory":
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                _reject("Candidate path kind changed")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _reject("Candidate path must be an unlinked regular file")

        before = _validate_snapshot(candidates[relative], entry.kind)
        current = _validate_snapshot(capture(path), entry.kind)
        if _candidate_security(before["security"]) != _candidate_security(current["security"]):
            _reject("Candidate security metadata changed")
        if not _streams_unchanged(before["streams"], current["streams"], entry,
                                  change, info.st_size):
            _reject("Candidate stream metadata changed")
        if before["extended_attributes"] != current["extended_attributes"]:
            _reject("Candidate extended-attribute metadata changed")

        changed_fields = {}
        for field in ("attributes", "created"):
            if before[field] != current[field]:
                changed_fields[field] = {"before": before[field], "after": current[field]}
        if entry.kind == "file" and before["written"] != current["written"]:
            changed_fields["written"] = {"before": before["written"], "after": current["written"]}

        if not changed_fields:
            continue
        if (before["attributes"] & ~_SUPPORTED_ATTRIBUTES
                or current["attributes"] & ~_SUPPORTED_ATTRIBUTES):
            _reject("Candidate uses unsupported complex file attributes")
        if not (_fully_comparable(before) and _fully_comparable(current)):
            _reject("Metadata-only candidate change contains unknown metadata")
        if entry.kind == "directory":
            _reject("Directory metadata-only changes are unsupported")

        if change is None:
            if not entry.writable or not scope.allows(plan.source / relative, write=True):
                _reject("Metadata-only candidate is outside the frozen writable scope")
            _read_regular_file(path, entry, plan)
            changes[relative] = stage.Change(relative, "modify", entry.sha256, entry.sha256)
        elif change.operation != "modify":
            _reject("Metadata delta conflicts with the host-verified operation")

        deltas.append({"relative": relative, "fields": changed_fields})

    metadata_digest = stage._hash(stage._encoded({
        "staged_digest": verified.staged_digest,
        "metadata_deltas": deltas,
    }))
    ordered = tuple(changes[name] for name in sorted(changes))
    return stage.VerifiedChanges(ordered, metadata_digest, state=verified.state)
