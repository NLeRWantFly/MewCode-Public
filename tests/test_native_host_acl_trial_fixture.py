"""Fixture correctness without administrator access or sandbox launches."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import prepare_native_host_acl_trial as trial


@pytest.mark.parametrize("lpac", [False, True])
@pytest.mark.parametrize("name", trial.CASE_NAMES)
def test_fixed_requests_keep_modes_and_exact_fixture_scope(monkeypatch, tmp_path, lpac, name):
    repository = tmp_path / "workspace" / "git-synthetic-repository"
    monkeypatch.setattr(trial.candidate, "_enumerate_repository_paths", lambda _: ([repository], []))
    monkeypatch.setattr(trial, "_existing_git_metadata", lambda _: [])
    monkeypatch.setattr(trial.candidate, "copied_git_dll_execute_paths", lambda _: [])
    monkeypatch.setattr(trial.appcontainer_policy, "build_probe_request", lambda **kwargs: kwargs)
    request = trial._request_for_case(
        name=name, lpac=lpac, fixture=tmp_path, repository=repository,
        workspace=repository.parent, profile=tmp_path / "profile",
        global_config=tmp_path / "profile" / "gitconfig", runtime_temp=repository / "temp",
        nul_probe=repository / "nul.exe", cwd_probe=repository / "cwd.exe",
        git_core=repository / "git.exe", powershell=tmp_path / "powershell.exe",
        powershell_script=tmp_path / "smoke.ps1",
    )
    assert request["lpac"] is lpac
    assert all(request[key] is lpac for key in trial.LPAC_CAPABILITIES)
    assert request["timeout_ms"] == 20_000
    assert all(path.is_relative_to(tmp_path) for path in request["write_paths"])
    if name == "powershell":
        assert repository / "synthetic-input.txt" in request["read_paths"]
        assert request["arguments"][:4] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
        assert request["arguments"][4] == trial._powershell_smoke_script(repository, repository / "synthetic-input.txt")
        assert "ExecutionPolicy" not in request["arguments"]


def test_controls_need_all_positive_evidence():
    controls = {"controls": [{"name": name, "ok": True} for name in trial.CASE_NAMES]}
    trial._require_host_controls(controls)
    controls["controls"][2]["ok"] = False
    with pytest.raises(RuntimeError, match="did not all succeed"):
        trial._require_host_controls(controls)
    with pytest.raises(RuntimeError, match="incomplete"):
        trial._require_host_controls({"controls": []})


def test_nonzero_or_missing_marker_is_not_a_success(monkeypatch, tmp_path):
    result = SimpleNamespace(returncode=0, stdout=b"wrong marker", stderr=b"")
    monkeypatch.setattr(trial.subprocess, "run", lambda *args, **kwargs: result)
    args = dict(name="test", command=["fixed.exe"], cwd=tmp_path,
                environment={}, expected_stdout="expected")
    assert trial._run_control(**args)["ok"] is False
    result.stdout = b"expected"
    assert trial._run_control(**args)["ok"] is True
    result.returncode = 1
    assert trial._run_control(**args)["ok"] is False


def test_script_preserves_unicode_and_quotes_without_mutating_host():
    text = trial._powershell_smoke_script(Path("C:/synthetic/中文'路径"), Path("C:/synthetic/input.txt"))
    assert "中文''路径" in text
    assert "Get-Content -LiteralPath" in text
    assert "New-PSDrive -Name MewWork -PSProvider FileSystem -Root" in text
    assert "Set-Location -LiteralPath 'MewWork:\\'" in text
    assert "Set-Acl" not in text
    source = Path(trial.__file__).read_text(encoding="utf-8")
    assert 'encoding="utf-8-sig"' in source
