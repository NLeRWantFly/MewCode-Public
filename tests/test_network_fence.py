from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import mewcode.sandbox.network_fence as fence
import scripts.build_network_fence as build_script


APP_SID = "S-1-15-2-1958404141-86561845-1752920682-3514627264-368642714-62675701-733520436"


def _candidate(directory: Path) -> fence.NetworkFenceCandidate:
    directory.mkdir(parents=True)
    executable = directory / fence.BINARY_NAME
    executable.write_bytes(b"synthetic native candidate")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest = {
        "schema": fence.CANDIDATE_SCHEMA,
        "binary": fence.BINARY_NAME,
        "sha256": digest,
        "source_sha256": {"MewCode.NetworkFence.cs": hashlib.sha256(
            (fence.ROOT / "native/windows/MewCode.NetworkFence.cs").read_bytes()).hexdigest()},
    }
    (directory / fence.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return fence.load_candidate(directory)


def test_build_compiles_x64_candidate_and_checks_clr_layouts(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    output = root / ".tmp/native-network-fence"
    source = Path(build_script.SOURCE)
    windows = tmp_path / "windows"
    compiler = windows / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b"mock compiler")
    monkeypatch.setattr(build_script, "ROOT", root)
    monkeypatch.setattr(build_script, "SOURCE", source)
    monkeypatch.setattr(build_script, "os", SimpleNamespace(name="nt", environ={"SystemRoot": str(windows)}))

    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == str(compiler):
            target = next(Path(arg[5:]) for arg in args if arg.startswith("/out:"))
            target.write_bytes(b"mock compiled x64 executable")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        assert args[1:] == ["--validate-layouts"]
        layout = {"schema": "mewcode-network-fence-layout-v1", "ok": True,
                  "layout": build_script.EXPECTED_LAYOUT}
        return SimpleNamespace(returncode=0, stdout=json.dumps(layout).encode(), stderr=b"")

    monkeypatch.setattr(build_script.subprocess, "run", run)
    manifest = build_script.build(Path(".tmp/native-network-fence"))

    compile_args, compile_kwargs = calls[0]
    assert compile_args[1:4] == ["/nologo", "/platform:x64", "/r:System.Web.Extensions.dll"]
    assert "/main:MewCode.Native.Windows.MewCodeNetworkFence" in compile_args
    assert compile_kwargs["timeout"] == 60
    assert calls[1][0] == [str(output / build_script.BINARY_NAME), "--validate-layouts"]
    assert manifest["sha256"] == hashlib.sha256((output / build_script.BINARY_NAME).read_bytes()).hexdigest()
    assert manifest["source_sha256"] == {source.name: hashlib.sha256(source.read_bytes()).hexdigest()}
    assert manifest["wfp_install_performed"] is False
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == manifest


def test_build_refuses_nonempty_output_without_overwriting(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    output = root / "candidate"
    output.mkdir(parents=True)
    marker = output / "keep.me"
    marker.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(build_script, "ROOT", root)
    monkeypatch.setattr(build_script, "os", SimpleNamespace(name="nt", environ={"SystemRoot": "unused"}))
    with pytest.raises(ValueError, match="nonempty"):
        build_script.build(output)
    assert marker.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("sid", [
    "S-1-5-21-100-200-300-1001",  # user SID
    "S-1-15-2-123-456",           # not a full profile SID
    APP_SID + "-1",               # too many subauthorities
    APP_SID.replace("1958404141", "4294967296"),  # uint32 overflow
    "S-1-15-2-01-2-3-4-5-6-7",    # noncanonical decimal
])
def test_appcontainer_sid_parser_rejects_non_profile_sids(sid):
    with pytest.raises(fence.NetworkFenceError) as error:
        fence.validate_appcontainer_sid(sid)
    assert error.value.code == "invalid_appcontainer_sid"


def test_host_invocations_are_argument_arrays_and_verify_executable_hash(tmp_path):
    candidate = _candidate(tmp_path / "bundle")
    report = tmp_path / "report with spaces.json"
    install = fence.prepare_install_call(candidate, APP_SID, report)
    status = fence.prepare_status_call(candidate, APP_SID)
    uninstall = fence.prepare_uninstall_call(candidate, APP_SID)

    assert install.argv == (str(candidate.executable), "--install", "--sid", APP_SID,
                            "--report", str(report))
    assert install.sha256 == candidate.sha256
    assert status.arguments == ("--status", "--sid", APP_SID)
    assert uninstall.arguments == ("--uninstall", "--sid", APP_SID)
    candidate.executable.write_bytes(b"tampered")
    with pytest.raises(fence.NetworkFenceError) as error:
        fence.prepare_status_call(candidate, APP_SID)
    assert error.value.code == "candidate_hash_mismatch"


def test_candidate_manifest_must_match_current_native_source(tmp_path):
    candidate = _candidate(tmp_path / "bundle")
    manifest_path = candidate.directory / fence.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_sha256"]["MewCode.NetworkFence.cs"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(fence.NetworkFenceError) as error:
        fence.load_candidate(candidate.directory)
    assert error.value.code == "candidate_source_mismatch"


def test_install_invocation_requires_new_report_path_and_absolute_path(tmp_path):
    candidate = _candidate(tmp_path / "bundle")
    with pytest.raises(fence.NetworkFenceError, match="absolute"):
        fence.prepare_install_call(candidate, APP_SID, Path("relative.json"))
    report = tmp_path / "exists.json"
    report.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(fence.NetworkFenceError, match="already exists"):
        fence.prepare_install_call(candidate, APP_SID, report)
    assert report.read_text(encoding="utf-8") == "do not overwrite"


def test_status_result_is_typed_and_bound_to_expected_appcontainer():
    raw = {
        "schema": fence.SCHEMA,
        "operation": "status",
        "ok": True,
        "error_code": "",
        "detail": "installed",
        "sid": APP_SID,
        "installed": True,
        "rules_complete": True,
        "loopback_exempt": True,
        "provider_present": True,
        "sublayer_present": True,
        "filters": {name: True for name in fence.FILTER_NAMES},
        "filter_keys": {name: "00000000-0000-0000-0000-000000000000" for name in fence.FILTER_NAMES},
        "requires_administrator": False,
    }
    result = fence.parse_status_result(json.dumps(raw), expected_sid=APP_SID)
    assert result.ok is True
    assert result.installed is True
    assert result.filters["allow_v6_60081"] is True
    assert result.sid == APP_SID

    raw["sid"] = "S-1-5-21-100-200-300-1001"
    with pytest.raises(fence.NetworkFenceError):
        fence.parse_status_result(json.dumps(raw), expected_sid=APP_SID)


def test_install_report_preserves_typed_admin_required_result(tmp_path):
    report = tmp_path / "install-report.json"
    report.write_text(json.dumps({
        "schema": fence.SCHEMA,
        "operation": "install",
        "ok": False,
        "error_code": "administrator_required",
        "detail": "Explicit elevated setup is required.",
        "sid": APP_SID,
        "requires_administrator": True,
    }), encoding="utf-8")
    result = fence.read_install_report(report, expected_sid=APP_SID)
    assert result.ok is False
    assert result.error_code == "administrator_required"
    assert result.requires_administrator is True
