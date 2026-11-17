"""Probe verdicts must distinguish a refused action from a broken test."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import probe_lpac_production as probe
from mewcode.sandbox.lpac_runtime import NativeExecutionReceipt


def matrix_report():
    from mewcode.sandbox.native_evidence import REQUIRED_CASES
    names = {name for group in REQUIRED_CASES.values() for name in group}
    records = []
    for fixture in ("c-volume", "e-volume"):
        for name in sorted(names):
            records.append({"fixture": fixture, "case": name, "ok": True, "failures": [],
                            "status": "cancelled" if name == "process_tree_cancelled" else "exited",
                            "exit_code": 1 if name in {"git_config_denied", "git_hook_write_denied"} else 0,
                            "cleanup_confirmed": True, "output_truncated": False,
                            "child_started": True, "child_reaped": True,
                            "backend": probe.lpac.BACKEND})
    return {"schema": probe.SCHEMA, "complete": True, "bundle_identity": "bundle",
            "machine_guid": "machine", "cases": records}


def test_full_summary_requires_cross_volume_and_complete_evidence():
    report = matrix_report()
    assert probe.report_summary(report)["full_matrix_passed"]
    report["cases"] = [r for r in report["cases"] if r["fixture"] == "c-volume"]
    summary = probe.report_summary(report)
    assert summary["selected_cases_passed"]
    assert not summary["full_matrix_passed"]
    assert summary["missing_evidence"] == ["compat_paths"]
    report["complete"] = False
    assert len(probe.report_summary(report)["missing_evidence"]) == 10


def test_summary_preserves_failed_and_unexecuted_cases():
    report = matrix_report()
    report["cases"][0].update(ok=False, status="not_run", failures=["not selected"])
    summary = probe.report_summary(report)
    assert summary["selected_cases_passed"]
    assert not summary["full_matrix_passed"]
    assert len(summary["not_run"]) == 1 and not summary["failed"]
    report["cases"][1].update(ok=False, failures=["actual native failure"])
    summary = probe.report_summary(report)
    assert not summary["selected_cases_passed"]
    assert not summary["full_matrix_passed"]
    assert len(summary["failed"]) == 1


def test_powershell_stdout_alone_cannot_prove_workspace_write_compatibility():
    report = matrix_report()
    report["cases"] = [r for r in report["cases"] if r["case"] != "workspace_file_roundtrip"]
    summary = probe.report_summary(report)
    assert not summary["full_matrix_passed"]
    assert "compat_powershell" in summary["missing_evidence"]


def test_added_assertions_never_erase_prior_failures():
    record = {"ok": False, "failures": ["cleanup unconfirmed", "stdout missing ATTEMPT"]}
    probe.require(record, True, "write not denied")
    assert not record["ok"]
    assert record["failures"] == ["cleanup unconfirmed", "stdout missing ATTEMPT"]


@pytest.mark.asyncio
async def test_mid_matrix_elevation_denial_stops_before_any_further_case(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "repository"
    repo.mkdir(parents=True)
    calls, persisted = [], []
    async def synthetic_case(checker, root, reads, name, command, **kwargs):
        calls.append(name)
        refused = name == "workspace_file_roundtrip"
        return {"case": name, "status": "denied" if refused else "exited",
                "detail": "Metadata broker elevation was not granted" if refused else "",
                "failures": ["startup denied"] if refused else [], "ok": not refused}
    monkeypatch.setattr(probe, "run_case", synthetic_case)
    monkeypatch.setattr(probe, "build_checker", lambda *a: None)
    with pytest.raises(probe.ProbeFailure, match="Elevation was not granted"):
        await probe.run_fixture("c-volume", {"workspace": workspace, "repo": repo,
            "base_python": tmp_path / "unused"}, tmp_path / "unused-git.exe", tmp_path,
            on_case=lambda record: persisted.append(dict(record)))
    assert calls == ["powershell_cjk", "workspace_file_roundtrip"]
    assert [r["case"] for r in persisted] == calls
    assert persisted[-1]["ok"] is False
    assert persisted[-1]["fixture"] == "c-volume"


@pytest.mark.parametrize("contents", [None, "not-a-pid", "0"])
def test_missing_or_invalid_pid_is_not_cleanup_success(tmp_path, contents):
    path = tmp_path / "child.pid"
    if contents is not None:
        path.write_text(contents, encoding="ascii")
    record = {"ok": True, "failures": []}
    probe.verify_child_reaped(record, path)
    assert not record["ok"]
    assert record.get("child_reaped") is not True


class NativeCall:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize("case_name", ["python_cjk", "python_unittest"])
async def test_python_matrix_does_not_inject_runtime_dependencies(tmp_path, monkeypatch, case_name):
    workspace = tmp_path / "workspace"
    repo = workspace / "repository"
    (repo / ".git/refs/heads").mkdir(parents=True)
    (repo / ".git/refs/heads/main").write_text("baseline")
    (repo / ".git/config").write_text("")
    observed = []

    class ReachedPython(Exception):
        pass

    async def capture(checker, root, reads, name, command, **kwargs):
        observed.append((name, reads))
        raise ReachedPython

    monkeypatch.setattr(probe, "run_case", capture)
    monkeypatch.setattr(probe, "build_checker", lambda *a: None)
    with pytest.raises(ReachedPython):
        await probe.run_fixture("c-volume", {"workspace": workspace, "repo": repo,
            "base_python": tmp_path / "must-not-be-injected"}, tmp_path / "git.exe", tmp_path,
            selected=frozenset({case_name}))
    assert observed == [(case_name, ())]


@pytest.mark.skipif(probe.os.name != "nt", reason="Windows wait API")
@pytest.mark.parametrize("wait,alive", [(0, False), (258, True)])
def test_open_process_handle_is_not_proof_of_liveness(monkeypatch, wait, alive):
    handle = 0x100000001
    kernel = SimpleNamespace(OpenProcess=NativeCall(handle), WaitForSingleObject=NativeCall(wait),
                             CloseHandle=NativeCall(1))
    monkeypatch.setattr(probe.ctypes, "WinDLL", lambda *a, **kw: kernel)
    assert probe._process_alive(42) is alive
    assert kernel.WaitForSingleObject.calls == [(handle, 0)]
    assert kernel.CloseHandle.calls == [(handle,)]
    assert kernel.OpenProcess.restype is probe.ctypes.c_void_p


@pytest.mark.skipif(probe.os.name != "nt", reason="Windows wait API")
def test_process_access_denied_is_unknown_not_reaped(monkeypatch, tmp_path):
    kernel = SimpleNamespace(OpenProcess=NativeCall(0), WaitForSingleObject=NativeCall(0),
                             CloseHandle=NativeCall(1))
    monkeypatch.setattr(probe.ctypes, "WinDLL", lambda *a, **kw: kernel)
    monkeypatch.setattr(probe.ctypes, "get_last_error", lambda: 5)
    path = tmp_path / "child.pid"
    path.write_text("42", encoding="ascii")
    record = {"ok": True, "failures": []}
    probe.verify_child_reaped(record, path)
    assert not record["ok"]
    assert "could not be verified" in record["failures"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,cleanup,truncated", [
    ("spawn_failed", True, False), ("exited", False, False), ("exited", True, True),
])
async def test_denial_probe_preserves_transport_cleanup_and_output_failures(
        monkeypatch, tmp_path, status, cleanup, truncated):
    async def fake_execute(*a, **kw):
        return NativeExecutionReceipt(status, exit_code=1, cleanup_confirmed=cleanup,
                                      output_truncated=truncated)
    monkeypatch.setattr(probe.lpac, "execute_lpac", fake_execute)
    monkeypatch.setattr(probe.lpac.NativeScope, "from_checker", lambda c: None)
    record = await probe.run_case(None, tmp_path, (), "git_config_denied", "synthetic",
                                  expect_exit=None, expect_stdout=("CONFIG_ATTEMPT",))
    probe.require(record, record["exit_code"] != 0, "write not denied")
    assert not record["ok"]
    assert len(record["failures"]) >= 2
