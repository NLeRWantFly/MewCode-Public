"""Read a bounded host-helper receipt proving staged cleanup completed."""
from __future__ import annotations

import json
from pathlib import Path
import re

SCHEMA = "mewcode-native-cleanup-v1"
BACKEND = "mewcode-windows-lpac"
MAX_CLEANUP_EVIDENCE_BYTES = 16 * 1024
_FIELDS = frozenset({
    "schema", "request_id", "request_sha256", "backend", "completed",
    "job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok",
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate cleanup evidence field")
        result[key] = value
    return result


def read_cleanup_evidence(path: Path, *, request_id: str, request_sha256: str) -> dict:
    """Return only exact identity and cleanup facts from a trusted helper receipt.

    This is evidence parsing only. It does not change a transaction ledger,
    authorize work, or infer cleanup from process exit or service loss.
    """
    from mewcode.sandbox.lpac_runtime import checked_path

    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Expected cleanup request identity is invalid")
    if not isinstance(request_sha256, str) or not _SHA256.fullmatch(request_sha256):
        raise ValueError("Expected cleanup request hash is invalid")
    try:
        checked = checked_path(Path(path), file=True)
        with checked.open("rb") as stream:
            raw = stream.read(MAX_CLEANUP_EVIDENCE_BYTES + 1)
    except Exception as error:
        raise ValueError("Cleanup evidence path is missing or unsafe") from error
    if not raw or len(raw) > MAX_CLEANUP_EVIDENCE_BYTES:
        raise ValueError("Cleanup evidence is empty or oversized")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Cleanup evidence is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("Cleanup evidence fields do not match the exact schema")
    if (payload.get("schema") != SCHEMA
            or payload.get("request_id") != request_id
            or payload.get("request_sha256") != request_sha256
            or payload.get("backend") != BACKEND
            or payload.get("completed") is not True
            or any(payload.get(name) is not True for name in (
                "job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok"))):
        raise ValueError("Cleanup evidence does not confirm this request was fully cleaned")
    return {key: payload[key] for key in (
        "schema", "request_id", "request_sha256", "backend", "completed",
        "job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok",
    )}
