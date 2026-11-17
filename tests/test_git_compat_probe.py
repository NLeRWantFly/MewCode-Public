import json
from pathlib import Path

import pytest

from scripts import probe_git_compat as probe
from scripts import probe_windows_path_roundtrip as rt


def invocation(**changes):
    receipt = {
        "exit_code": 0, "appcontainer_verified": True, "lpac_verified": True,
        "lpac_verification_method": "access_check", "lpac_access_mask": 2,
        "capabilities": rt.CAPABILITIES[:], "timed_out": False, "cancelled": False,
        "stdout_truncated": False, "stderr_truncated": False,
        "stdout": "", "stderr": "", "error": "",
        "job_cleanup_ok": True, "profile_cleanup_ok": True, "acl_cleanup_ok": True,
    }
    receipt.update(changes)
    return {"receipt": receipt, "exit_code": receipt["exit_code"],
            "timed_out": False, "cancelled": False, "start_error_type": None,
            "output_oversized": False}


def test_output_requires_real_expected_content():
    repo = Path("C:/synthetic")
    assert probe.output_passed("version", 0, probe.VERSION, "", repo, container=True)
    assert not probe.output_passed("version", 0, "", "", repo, container=True)
    assert not probe.output_passed("status", 0, "", "", repo, container=True)
    assert probe.output_passed("status", 0, "?? sample.txt\r\n", "", repo, container=True)
    assert not probe.output_passed("status", False, "?? sample.txt\n", "", repo, container=True)
    assert not probe.output_passed("status", 0, "?? sample.txt\n", "warning", repo, container=True)
    assert not probe.output_passed("unknown", 0, "", "", repo, container=True)


@pytest.mark.parametrize("changes", [
    {"job_cleanup_ok": False}, {"acl_cleanup_ok": None}, {"profile_cleanup_ok": 1},
    {"appcontainer_verified": False}, {"lpac_verified": False},
    {"lpac_access_mask": 3}, {"lpac_access_mask": True}, {"capabilities": []},
    {"timed_out": True}, {"cancelled": True}, {"stdout_truncated": True},
    {"stderr_truncated": True}, {"error": "fault"}, {"stdout": None},
    {"stderr": None}, {"exit_code": False},
])
def test_unknown_or_invalid_receipt_is_not_complete(changes):
    assert not probe.helper_evidence(invocation(**changes), lpac=True)["complete"]


def test_expected_original_failure_is_observation_not_compatibility():
    value = invocation(exit_code=128, stderr="fatal: NUL")
    assert probe.helper_evidence(value, lpac=True)["complete"]
    assert not probe.output_passed("version", 128, "", "fatal: NUL", Path("C:/x"), container=True)


def test_host_identity_does_not_satisfy_lpac():
    value = invocation(lpac_access_mask=3, capabilities=[])
    assert probe.helper_evidence(value, lpac=False)["complete"]
    assert not probe.helper_evidence(value, lpac=True)["complete"]


def test_duplicate_json_and_nonfinite_rejected(tmp_path):
    path = tmp_path / "manifest.json"
    for text in ('{"schema":1,"schema":2}', '{"n":NaN}', '[]'):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            probe.read_json(path)


def test_no_run_does_not_create_fixture(monkeypatch):
    monkeypatch.setattr(rt, "_new_fixture", lambda: pytest.fail("created fixture"))
    assert probe.main([]) == 0
    with pytest.raises(SystemExit) as error:
        probe.main(["--assert-compatible"])
    assert error.value.code == 2


def test_build_must_be_authorized_project_scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    expected = tmp_path / ".tmp" / probe.BUILD_NAME
    expected.mkdir(parents=True)
    assert probe.checked_build(expected) == expected
    with pytest.raises(ValueError):
        probe.checked_build(tmp_path)


@pytest.mark.parametrize("relative", ["../outside.exe", "C:/outside.exe", "/outside.exe"])
def test_manifest_path_escape_rejected(tmp_path, relative):
    manifest = {"schema": "mewcode-git-compat-build-v1", "production_authorized": False,
                "source_commit": probe.SOURCE_COMMIT, "sdk_commit": probe.SDK_COMMIT,
                "files": {relative: "0" * 64}, "sources": {"anything": "0" * 64}}
    with pytest.raises(ValueError):
        probe.verify_manifest(tmp_path, manifest)


def test_hash_drift_rejected(tmp_path):
    path = tmp_path / "artifact.exe"
    path.write_bytes(b"original")
    digest = probe.sha(path)
    path.write_bytes(b"changed")
    manifest = {"schema": "mewcode-git-compat-build-v1", "production_authorized": False,
                "source_commit": probe.SOURCE_COMMIT, "sdk_commit": probe.SDK_COMMIT,
                "files": {"artifact.exe": digest}}
    with pytest.raises(ValueError, match="drift"):
        probe.verify_manifest(tmp_path, manifest)


def test_live_protocol_does_not_accept_an_unrestricted_child():
    host = "appcontainer=0\noriginal_dos_ok=1 error=0\nMEWCODE_GIT_COMPAT_LIVE_OK same_file=1 buffer=1 invalid_fd=1\n"
    assert probe.output_passed("live", 0, host, "", Path("C:/x"), container=False)
    assert not probe.output_passed("live", 0, host, "", Path("C:/x"), container=True)
