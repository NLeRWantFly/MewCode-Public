"""Evidence-driven native gate: five states, fail-closed on every doubt."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mewcode.sandbox import native_gate, windows
from mewcode.sandbox.native_gate import (
    ATTESTATION_NAME, ATTESTATION_SCHEMA, ENABLEMENT_NAME, ENABLEMENT_SCHEMA,
    REQUIRED_EVIDENCE, NativeGateState, native_gate_status,
)
from scripts import manage_native_runtime as installer

MACHINE = "TEST-MACHINE-GUID"
REPORT = "docs/auto-release-baseline-20260926.md"


def test_broker_bundle_needs_metadata_acceptance_not_only_old_matrix(data_root):
    target = _install_bundle(data_root)
    _declare_broker(target)
    _attestation(target)
    status = native_gate_status(target)
    assert status.state is NativeGateState.PENDING
    assert {"metadata_preserved", "metadata_protocol_quick", "metadata_protocol_lifetime"} <= set(
        status.missing_evidence)


def test_metadata_acceptance_requires_every_case_and_exact_binding():
    from mewcode.sandbox.native_evidence import METADATA_CASES, METADATA_SCHEMA, evidence_passes
    report = {"schema": METADATA_SCHEMA, "complete": True, "ok": True,
              "bundle_identity": "bundle", "machine_guid": "machine",
              "cases": [{"case": name, "ok": True} for name in sorted(METADATA_CASES)]}
    assert evidence_passes(report, "metadata_preserved", "bundle", "machine")
    assert not evidence_passes(report, "metadata_preserved", "wrong", "machine")
    report["cases"].pop()
    assert not evidence_passes(report, "metadata_preserved", "bundle", "machine")


def test_metadata_protocol_requirements_are_capability_scoped_and_valid_reports_pass(data_root):
    from mewcode.sandbox.native_evidence import evidence_passes
    legacy = _install_bundle(data_root)
    assert native_gate.required_evidence(legacy) == REQUIRED_EVIDENCE

    target = _install_bundle(data_root / "broker-package")
    broker_sha256 = _declare_broker(target)
    expected = (*REQUIRED_EVIDENCE, "metadata_preserved",
                "metadata_protocol_quick", "metadata_protocol_lifetime")
    assert native_gate.required_evidence(target) == expected

    for key, mode in (("metadata_protocol_quick", "quick"),
                      ("metadata_protocol_lifetime", "lifetime")):
        report = json.loads(_metadata_protocol_report(target, mode).read_text(encoding="utf-8"))
        assert evidence_passes(report, key, _identity(target), MACHINE,
                               broker_sha256=broker_sha256)
        assert not evidence_passes(report, key, _identity(target), MACHINE,
                                   broker_sha256="0" * 64)
        if mode == "lifetime":
            report["cases"][0]["exit_code"] = 124
            assert evidence_passes(report, key, _identity(target), MACHINE,
                                   broker_sha256=broker_sha256)
            report["cases"][0]["exit_code"] = 123
            assert not evidence_passes(report, key, _identity(target), MACHINE,
                                       broker_sha256=broker_sha256)


@pytest.mark.parametrize("change", [
    "missing_case", "failed_case", "duplicate_case", "unknown_case", "wrong_mode",
    "wrong_bundle", "wrong_machine", "wrong_broker", "incomplete", "error_field",
    "unknown_case_field", "duplicate_field", "elapsed_too_short", "elapsed_too_long",
    "exit_code_wrong",
])
def test_metadata_protocol_evidence_rejects_incomplete_or_forged_reports(data_root, change):
    target = _install_bundle(data_root / "broker-package")
    _declare_broker(target)
    required = native_gate.required_evidence(target)
    _attestation(target, keys=required)
    key = "metadata_protocol_lifetime" if change.startswith("elapsed") or change == "exit_code_wrong" else "metadata_protocol_quick"
    record_path = target / ATTESTATION_NAME
    attestation = json.loads(record_path.read_text(encoding="utf-8"))
    evidence = attestation["evidence"][key]
    report_path = Path(evidence["report"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if change == "missing_case":
        report["cases"].pop()
    elif change == "failed_case":
        report["cases"][0]["ok"] = False
    elif change == "duplicate_case":
        report["cases"].append(dict(report["cases"][0]))
    elif change == "unknown_case":
        report["cases"][0]["case"] = "unrecognized_case"
    elif change == "wrong_mode":
        report["mode"] = "lifetime"
    elif change == "wrong_bundle":
        report["bundle_identity"] = "wrong-bundle"
    elif change == "wrong_machine":
        report["machine_guid"] = "wrong-machine"
    elif change == "wrong_broker":
        report["helper_sha256"] = "0" * 64
    elif change == "incomplete":
        report["complete"] = False
    elif change == "error_field":
        report["error"] = "SyntheticFailure: not a successful report"
    elif change == "unknown_case_field":
        report["cases"][0]["unexpected"] = "not part of the probe schema"
    elif change == "elapsed_too_short":
        report["cases"][0]["elapsed_seconds"] = 30.0
    elif change == "elapsed_too_long":
        report["cases"][0]["elapsed_seconds"] = 1300.0
    elif change == "exit_code_wrong":
        report["cases"][0]["exit_code"] = 0
    raw = json.dumps(report)
    if change == "duplicate_field":
        raw = raw.replace('"complete": true', '"complete": true, "complete": true', 1)
    report_path.write_text(raw, encoding="utf-8")
    evidence["sha256"] = _sha256(report_path.read_bytes())
    record_path.write_text(json.dumps(attestation, sort_keys=True), encoding="utf-8")

    status = native_gate_status(target)

    assert status.state is NativeGateState.PENDING
    assert key in status.missing_evidence


def test_valid_broker_protocol_reports_are_required_for_eligibility(data_root):
    target = _install_bundle(data_root / "broker-package")
    _declare_broker(target)
    _attestation(target, keys=native_gate.required_evidence(target))
    status = native_gate_status(target)
    assert status.state is NativeGateState.ELIGIBLE
    assert status.missing_evidence == ()


def test_installer_records_broker_protocol_through_real_gate_path(data_root, monkeypatch):
    target = _install_bundle(data_root)
    _declare_broker(target)
    monkeypatch.setattr(installer, "ROOT", data_root)
    baseline = _report(target)
    paths = {key: baseline for key in REQUIRED_EVIDENCE}
    paths.update(metadata_preserved=_metadata_preserved_report(target),
                 metadata_protocol_quick=_metadata_protocol_report(target, "quick"),
                 metadata_protocol_lifetime=_metadata_protocol_report(target, "lifetime"))
    quick_path = paths["metadata_protocol_quick"]
    quick = json.loads(quick_path.read_text(encoding="utf-8"))
    oversized = next(case for case in quick["cases"] if case["case"] == "oversized_frame_denied")
    oversized["exit_code"] = 0
    quick_path.write_text(json.dumps(quick), encoding="utf-8")
    outcome = installer.record_attestation(data_root, [
        (key, path.relative_to(data_root).as_posix()) for key, path in paths.items()])
    assert outcome["gate_state"] == "eligible"
    assert native_gate_status(target).state is NativeGateState.ELIGIBLE
    oversized["exit_code"] = 124
    quick_path.write_text(json.dumps(quick), encoding="utf-8")
    with pytest.raises(installer.InstallerError, match="Invalid acceptance evidence"):
        installer.record_attestation(data_root, [
            ("metadata_protocol_quick", quick_path.relative_to(data_root).as_posix())])
    assert native_gate_status(target).state is NativeGateState.PENDING


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _install_bundle(data_root: Path, *, production_enabled: bool = False) -> Path:
    """A synthetic but fully hash-consistent installed bundle."""
    target = data_root / "native" / "windows-lpac-v1"
    git_dir = target / "git"
    git_dir.mkdir(parents=True)
    helper = b"synthetic-helper-bytes-v1"
    git_exe = b"synthetic-git-bytes-v1"
    (target / "MewCode.AppContainerRuntime.exe").write_bytes(helper)
    (git_dir / "git.exe").write_bytes(git_exe)
    manifest = {
        "schema": "mewcode-lpac-bundle-v1",
        "sha256": _sha256(helper),
        "production_enabled": production_enabled,
        "bundle_version": "test.1",
        "files": {"git/git.exe": _sha256(git_exe)},
    }
    (target / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return target


def _identity(target: Path) -> str:
    return native_gate.installed_bundle_identity(target)


def _declare_broker(target: Path) -> str:
    broker = b"synthetic-metadata-broker-bytes-v1"
    broker_path = target / "broker" / "MewCode.MetadataBroker.exe"
    broker_path.parent.mkdir()
    broker_path.write_bytes(broker)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["protocols"] = ["mewcode-metadata-broker-v1"]
    manifest["files"]["broker/MewCode.MetadataBroker.exe"] = _sha256(broker)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return _sha256(broker)


def _report(target: Path) -> Path:
    from mewcode.sandbox.native_evidence import REQUIRED_CASES, SCHEMA
    cases = []
    for fixture in ("c-volume", "e-volume"):
        for name in sorted({name for names in REQUIRED_CASES.values() for name in names}):
            cases.append({"fixture": fixture, "case": name, "ok": True, "failures": [],
                          "status": "cancelled" if name == "process_tree_cancelled" else "exited",
                          "exit_code": 1 if name in {"git_config_denied", "git_hook_write_denied"} else 0,
                          "cleanup_confirmed": True, "child_reaped": True, "child_started": True,
                          "backend": "mewcode-windows-lpac", "output_truncated": False})
    path = target.parents[1] / "acceptance.json"
    path.write_text(json.dumps({"schema": SCHEMA, "bundle_identity": _identity(target),
                                "machine_guid": MACHINE, "complete": True, "cases": cases}), encoding="utf-8")
    return path


def _metadata_protocol_report(target: Path, mode: str) -> Path:
    from mewcode.sandbox.native_evidence import (
        METADATA_PROTOCOL_LIFETIME_CASES, METADATA_PROTOCOL_QUICK_CASES, METADATA_PROTOCOL_SCHEMA,
    )
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    broker_sha256 = manifest["files"]["broker/MewCode.MetadataBroker.exe"]
    if mode == "quick":
        cases = [{"case": name, "ok": True}
                 for name in sorted(METADATA_PROTOCOL_QUICK_CASES)]
    else:
        cases = [{"case": name, "ok": True, "elapsed_seconds": 1200.0}
                 for name in sorted(METADATA_PROTOCOL_LIFETIME_CASES)]
    path = target.parents[1] / f"metadata-protocol-{mode}.json"
    path.write_text(json.dumps({
        "schema": METADATA_PROTOCOL_SCHEMA, "mode": mode,
        "bundle_identity": _identity(target), "machine_guid": MACHINE,
        "helper_sha256": broker_sha256, "complete": True, "cases": cases,
    }, sort_keys=True), encoding="utf-8")
    return path


def _metadata_preserved_report(target: Path) -> Path:
    from mewcode.sandbox.native_evidence import METADATA_CASES, METADATA_SCHEMA
    path = target.parents[1] / "metadata-preserved.json"
    path.write_text(json.dumps({
        "schema": METADATA_SCHEMA, "complete": True, "ok": True,
        "bundle_identity": _identity(target), "machine_guid": MACHINE,
        "cases": [{"case": name, "ok": True} for name in sorted(METADATA_CASES)],
    }, sort_keys=True), encoding="utf-8")
    return path


def _attestation(target: Path, keys=REQUIRED_EVIDENCE, *, identity=None, machine=MACHINE) -> None:
    reports = {}
    evidence = {}
    for key in keys:
        if key == "metadata_protocol_quick":
            report = _metadata_protocol_report(target, "quick")
        elif key == "metadata_protocol_lifetime":
            report = _metadata_protocol_report(target, "lifetime")
        elif key == "metadata_preserved":
            report = _metadata_preserved_report(target)
        else:
            if "baseline" not in reports:
                reports["baseline"] = _report(target)
            report = reports["baseline"]
        evidence[key] = {"result": "pass", "report": str(report),
                         "sha256": _sha256(report.read_bytes())}
    (target / ATTESTATION_NAME).write_text(json.dumps({
        "schema": ATTESTATION_SCHEMA,
        "bundle_identity": identity if identity is not None else _identity(target),
        "machine_guid": machine,
        "recorded_utc": "2026-09-27T00:00:00Z",
        "evidence": evidence,
    }, sort_keys=True), encoding="utf-8")


def _enablement(target: Path, *, identity=None, machine=MACHINE) -> None:
    (target / ENABLEMENT_NAME).write_text(json.dumps({
        "schema": ENABLEMENT_SCHEMA,
        "bundle_identity": identity if identity is not None else _identity(target),
        "machine_guid": machine,
        "enabled_utc": "2026-09-27T00:00:00Z",
        "operator": "test",
    }, sort_keys=True), encoding="utf-8")


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "data-root"
    root.mkdir()
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(root))
    monkeypatch.setattr(native_gate, "machine_guid", lambda: MACHINE)
    return root


def test_not_installed_state(data_root: Path) -> None:
    status = native_gate_status()
    assert status.state is NativeGateState.NOT_INSTALLED
    assert "not installed" in status.reason
    assert status.missing_evidence == ()
    assert windows.native_runtime_ready() is False
    assert windows.native_fastpath_ready() is False


def test_verified_bundle_without_attestation_is_pending(data_root: Path) -> None:
    _install_bundle(data_root)
    status = native_gate_status()
    assert status.state is NativeGateState.PENDING
    assert status.missing_evidence == REQUIRED_EVIDENCE
    assert windows.native_runtime_ready() is False


def test_partial_attestation_names_exact_missing_evidence(data_root: Path) -> None:
    target = _install_bundle(data_root)
    partial = REQUIRED_EVIDENCE[:3]
    _attestation(target, keys=partial)
    status = native_gate_status()
    assert status.state is NativeGateState.PENDING
    assert status.missing_evidence == tuple(key for key in REQUIRED_EVIDENCE if key not in partial)
    for key in status.missing_evidence:
        assert key in status.reason
    assert windows.native_runtime_ready() is False


def test_complete_attestation_is_eligible_but_gates_stay_closed(data_root: Path) -> None:
    target = _install_bundle(data_root)
    _attestation(target)
    status = native_gate_status()
    assert status.state is NativeGateState.ELIGIBLE
    assert status.missing_evidence == ()
    assert "enable" in status.reason
    assert windows.native_runtime_ready() is False
    assert windows.native_fastpath_ready() is False


def test_enablement_opens_both_gates(data_root: Path) -> None:
    target = _install_bundle(data_root)
    _attestation(target)
    _enablement(target)
    status = native_gate_status()
    assert status.state is NativeGateState.ENABLED
    assert windows.native_runtime_ready() is True
    assert windows.native_fastpath_ready() is True


def test_bundle_drift_invalidates_gate(data_root: Path) -> None:
    target = _install_bundle(data_root)
    _attestation(target)
    _enablement(target)
    (target / "git" / "git.exe").write_bytes(b"tampered-git-bytes")
    status = native_gate_status()
    assert status.state is NativeGateState.INVALID
    assert "verification" in status.reason
    assert windows.native_runtime_ready() is False


def test_attestation_bound_to_other_bundle_is_invalid(data_root: Path) -> None:
    target = _install_bundle(data_root)
    _attestation(target, identity="0" * 64)
    _enablement(target, identity="0" * 64)
    status = native_gate_status()
    assert status.state is NativeGateState.INVALID
    assert "different bundle identity" in status.reason


def test_attestation_bound_to_other_machine_is_invalid(data_root: Path) -> None:
    target = _install_bundle(data_root)
    _attestation(target, machine="OTHER-MACHINE")
    status = native_gate_status()
    assert status.state is NativeGateState.INVALID
    assert "not bound to this machine" in status.reason


def test_enablement_without_attestation_is_invalid(data_root: Path) -> None:
    target = _install_bundle(data_root)
    _enablement(target)
    status = native_gate_status()
    assert status.state is NativeGateState.INVALID
    assert "without an acceptance attestation" in status.reason


def test_enablement_bound_to_other_bundle_is_invalid(data_root: Path) -> None:
    target = _install_bundle(data_root)
    _attestation(target)
    _enablement(target, identity="f" * 64)
    status = native_gate_status()
    assert status.state is NativeGateState.INVALID
    assert "different bundle or machine" in status.reason


def test_unknown_evidence_key_is_invalid(data_root: Path) -> None:
    target = _install_bundle(data_root)
    _attestation(target)
    record = json.loads((target / ATTESTATION_NAME).read_text(encoding="utf-8"))
    record["evidence"]["made_up_key"] = {"result": "pass", "report": REPORT}
    (target / ATTESTATION_NAME).write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    status = native_gate_status()
    assert status.state is NativeGateState.INVALID
    assert "unknown evidence keys" in status.reason


def test_manifest_self_declaration_never_opens_gate(data_root: Path) -> None:
    # A bundle claiming production_enabled=true must not change anything.
    _install_bundle(data_root, production_enabled=True)
    status = native_gate_status()
    assert status.state is NativeGateState.PENDING
    assert windows.native_runtime_ready() is False


def test_unreadable_machine_identity_fails_closed(data_root: Path, monkeypatch) -> None:
    target = _install_bundle(data_root)
    _attestation(target)
    _enablement(target)
    monkeypatch.setattr(native_gate, "machine_guid", lambda: "")
    status = native_gate_status()
    assert status.state is NativeGateState.INVALID
    assert windows.native_runtime_ready() is False


def test_installer_attestation_enable_disable_flow(data_root: Path, monkeypatch) -> None:
    target = _install_bundle(data_root)
    report = _report(target)
    monkeypatch.setattr(installer, "ROOT", data_root)
    result = installer.record_attestation(
        data_root, [(key, report.name) for key in REQUIRED_EVIDENCE[:5]])
    assert result["gate_state"] == "pending"
    result = installer.record_attestation(
        data_root, [(key, report.name) for key in REQUIRED_EVIDENCE[5:]])
    assert result["gate_state"] == "eligible"
    enabled = installer.enable_gate(data_root, note="acceptance complete")
    assert enabled["enabled"] is True
    assert enabled["gate_state"] == "enabled"
    assert windows.native_runtime_ready() is True
    disabled = installer.disable_gate(data_root)
    assert disabled["enablement_removed"] is True
    assert disabled["gate_state"] == "eligible"
    assert windows.native_runtime_ready() is False


def test_enable_refused_before_attestation(data_root: Path) -> None:
    _install_bundle(data_root)
    with pytest.raises(installer.InstallerError, match="Refusing to enable"):
        installer.enable_gate(data_root, note="too early")


def test_prose_is_not_acceptance_evidence(data_root: Path) -> None:
    _install_bundle(data_root)
    with pytest.raises(installer.InstallerError, match="Invalid acceptance evidence"):
        installer.record_attestation(data_root, [(REQUIRED_EVIDENCE[0], REPORT)])
    assert native_gate_status().state is NativeGateState.PENDING


@pytest.mark.parametrize("change", ["modified", "deleted", "old_reference"])
def test_evidence_drift_closes_an_enabled_gate(data_root: Path, change: str) -> None:
    target = _install_bundle(data_root)
    _attestation(target)
    _enablement(target)
    assert native_gate_status().ready
    report = target.parents[1] / "acceptance.json"
    if change == "modified":
        report.write_text(report.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    elif change == "deleted":
        report.unlink()
    else:
        record = json.loads((target / ATTESTATION_NAME).read_text(encoding="utf-8"))
        record["evidence"] = {key: {"result": "pass", "report": REPORT}
                              for key in REQUIRED_EVIDENCE}
        (target / ATTESTATION_NAME).write_text(json.dumps(record), encoding="utf-8")
    assert not native_gate_status().ready


@pytest.mark.parametrize("change", ["failed", "duplicate", "wrong_bundle", "wrong_machine",
                                     "missing_e_volume", "missing_cancel", "unknown_cleanup",
                                     "incomplete", "child_never_started"])
def test_incomplete_live_report_cannot_attest(data_root: Path, monkeypatch, change: str) -> None:
    target = _install_bundle(data_root)
    path = _report(target)
    report = json.loads(path.read_text(encoding="utf-8"))
    if change == "failed":
        for case in report["cases"]:
            case["ok"] = False
    elif change == "duplicate":
        report["cases"].append(report["cases"][0])
    elif change == "wrong_bundle":
        report["bundle_identity"] = "other"
    elif change == "wrong_machine":
        report["machine_guid"] = "other"
    elif change == "missing_e_volume":
        report["cases"] = [c for c in report["cases"] if c["fixture"] != "e-volume"]
    elif change == "missing_cancel":
        report["cases"] = [c for c in report["cases"] if c["case"] != "process_tree_cancelled"]
    elif change == "incomplete":
        report["complete"] = False
    elif change == "child_never_started":
        for case in report["cases"]:
            case.pop("child_started", None)
    else:
        for case in report["cases"]:
            case["cleanup_confirmed"] = None
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(installer, "ROOT", data_root)
    with pytest.raises(installer.InstallerError, match="Invalid acceptance evidence"):
        installer.record_attestation(data_root, [(key, path.name) for key in REQUIRED_EVIDENCE])
    assert not (target / ATTESTATION_NAME).exists()


def test_inaccessible_bundle_is_not_reported_as_uninstalled(data_root: Path, monkeypatch) -> None:
    target = _install_bundle(data_root)
    original = Path.stat
    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("synthetic denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", denied)
    status = native_gate_status()
    assert status.state is NativeGateState.INVALID
    assert "inaccessible" in status.reason


def test_record_attestation_rejects_unknown_key_and_missing_report(data_root: Path) -> None:
    _install_bundle(data_root)
    with pytest.raises(installer.InstallerError, match="Unknown evidence key"):
        installer.record_attestation(data_root, [("not_a_real_key", REPORT)])
    with pytest.raises(installer.InstallerError, match="does not exist"):
        installer.record_attestation(data_root, [(REQUIRED_EVIDENCE[0], "docs/no-such-file.md")])
    with pytest.raises(installer.InstallerError, match="repo-relative"):
        installer.record_attestation(data_root, [(REQUIRED_EVIDENCE[0], "../outside.md")])


def test_checker_deny_message_names_gate_state(data_root: Path, tmp_path: Path) -> None:
    _install_bundle(data_root)
    from mewcode.permissions import (
        DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine,
    )
    from mewcode.tools.bash import Bash
    checker = PermissionChecker(
        DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    checker.requires_command_isolation = True  # Child scope cannot use the primary's reviewed host route.
    decision = checker.check(Bash(), {"command": "python -m pytest"})
    assert decision.effect == "deny"
    assert decision.source == "native_boundary_unverified"
    assert "AUTO_BOUNDARY_DENIED" in decision.reason
    assert "pending" in decision.reason
