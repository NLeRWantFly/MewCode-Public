from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mewcode.sandbox.cleanup_evidence import (
    BACKEND,
    MAX_CLEANUP_EVIDENCE_BYTES,
    SCHEMA,
    read_cleanup_evidence,
)


REQUEST_ID = "3b80552a33c04ee3b5034804662b9c6b"
REQUEST_SHA256 = "a" * 64


def record(**overrides):
    value = {
        "schema": SCHEMA,
        "request_id": REQUEST_ID,
        "request_sha256": REQUEST_SHA256,
        "backend": BACKEND,
        "completed": True,
        "job_cleanup_ok": True,
        "profile_cleanup_ok": True,
        "acl_cleanup_ok": True,
    }
    value.update(overrides)
    return value


def write_record(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    return path


def test_valid_cleanup_evidence_returns_only_the_bound_cleanup_facts(tmp_path: Path):
    path = write_record(tmp_path / "receipt.json", record())

    result = read_cleanup_evidence(path, request_id=REQUEST_ID, request_sha256=REQUEST_SHA256)

    assert result == record()
    assert set(result) == {
        "schema", "request_id", "request_sha256", "backend", "completed",
        "job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok",
    }


def test_missing_and_partial_evidence_are_not_cleanup_confirmation(tmp_path: Path):
    with pytest.raises(ValueError, match="missing or unsafe"):
        read_cleanup_evidence(tmp_path / "missing.json", request_id=REQUEST_ID,
                              request_sha256=REQUEST_SHA256)

    path = tmp_path / "partial.json"
    path.write_bytes(b'{"schema":"mewcode-native-cleanup-v1"')
    with pytest.raises(ValueError):
        read_cleanup_evidence(path, request_id=REQUEST_ID, request_sha256=REQUEST_SHA256)

    write_record(path, record(profile_cleanup_ok=False))
    with pytest.raises(ValueError, match="fully cleaned"):
        read_cleanup_evidence(path, request_id=REQUEST_ID, request_sha256=REQUEST_SHA256)


def test_oversized_duplicate_unknown_and_incomplete_records_are_rejected(tmp_path: Path):
    path = tmp_path / "receipt.json"
    path.write_bytes(b" " * (MAX_CLEANUP_EVIDENCE_BYTES + 1))
    with pytest.raises(ValueError, match="oversized"):
        read_cleanup_evidence(path, request_id=REQUEST_ID, request_sha256=REQUEST_SHA256)

    path.write_text('{"schema":"mewcode-native-cleanup-v1","schema":"mewcode-native-cleanup-v1"}',
                    encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        read_cleanup_evidence(path, request_id=REQUEST_ID, request_sha256=REQUEST_SHA256)

    write_record(path, record(extra="not part of the bounded contract"))
    with pytest.raises(ValueError, match="exact schema"):
        read_cleanup_evidence(path, request_id=REQUEST_ID, request_sha256=REQUEST_SHA256)

    value = record()
    value.pop("acl_cleanup_ok")
    write_record(path, value)
    with pytest.raises(ValueError, match="exact schema"):
        read_cleanup_evidence(path, request_id=REQUEST_ID, request_sha256=REQUEST_SHA256)


@pytest.mark.parametrize("identity", [
    {"request_id": "4b80552a33c04ee3b5034804662b9c6b"},
    {"request_sha256": "b" * 64},
])
def test_receipt_must_match_both_request_identity_components(tmp_path: Path, identity: dict):
    path = write_record(tmp_path / "receipt.json", record())
    with pytest.raises(ValueError, match="does not confirm"):
        read_cleanup_evidence(path,
                              request_id=identity.get("request_id", REQUEST_ID),
                              request_sha256=identity.get("request_sha256", REQUEST_SHA256))


@pytest.mark.parametrize("field", ["job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok"])
def test_each_cleanup_fact_must_be_exact_true(tmp_path: Path, field: str):
    path = write_record(tmp_path / "receipt.json", record(**{field: False}))
    with pytest.raises(ValueError, match="fully cleaned"):
        read_cleanup_evidence(path, request_id=REQUEST_ID, request_sha256=REQUEST_SHA256)

    write_record(path, record(**{field: 1}))
    with pytest.raises(ValueError, match="fully cleaned"):
        read_cleanup_evidence(path, request_id=REQUEST_ID, request_sha256=REQUEST_SHA256)


def test_hardlinked_evidence_is_rejected(tmp_path: Path):
    path = write_record(tmp_path / "receipt.json", record())
    alias = tmp_path / "alias.json"
    try:
        os.link(path, alias)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable in this synthetic test directory")

    with pytest.raises(ValueError, match="missing or unsafe"):
        read_cleanup_evidence(alias, request_id=REQUEST_ID, request_sha256=REQUEST_SHA256)


def test_helper_source_creates_a_noninheritable_new_receipt_and_flushes_after_execution():
    source = (Path(__file__).resolve().parents[1] /
              "native/windows/MewCode.AppContainerRuntime.cs").read_text(encoding="utf-8")
    assert "public string cleanup_receipt_path { get; set; }" in source
    assert "FileMode.CreateNew, FileAccess.Write, FileShare.Read" in source
    assert "SetHandleInformation(stream.SafeFileHandle.DangerousGetHandle(), HandleFlagInherit, 0)" in source
    assert "stream.Flush(true)" in source
    assert 'schema = "mewcode-native-cleanup-v1"' in source
    assert "request_sha256 = requestSha256" in source
    assert "PathsOverlap(path, grant.Path)" in source
    assert "WriteCleanupEvidence(cleanupEvidence, requestId, requestSha256, receipt)" in source
