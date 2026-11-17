from pathlib import Path
import subprocess

from scripts.diagnose_powershell_runtime import (
    ENVIRONMENT_KEYS,
    PATHEXT_CONTROL,
    build_environment,
    summarize_process,
)


def test_host_control_environment_is_exact_and_has_fixed_pathext(monkeypatch) -> None:
    monkeypatch.setenv("MEWCODE_SYNTHETIC_API_KEY", "not inherited")
    system_root = Path("C:/Windows")
    synthetic_fixture = Path("C:/synthetic-powershell-fixture")
    clean = build_environment(system_root, synthetic_fixture, pathext=False)
    with_extension = build_environment(system_root, synthetic_fixture, pathext=True)

    assert set(clean) == ENVIRONMENT_KEYS
    assert "PATHEXT" not in clean
    assert "MEWCODE_SYNTHETIC_API_KEY" not in clean
    assert with_extension == {**clean, "PATHEXT": PATHEXT_CONTROL}


def test_completed_pipeline_without_native_marker_is_not_accepted() -> None:
    result = subprocess.CompletedProcess(
        args=["synthetic-payload.exe", "powershell-init"],
        returncode=2,
        stdout="PS_INIT_PIPELINE_INVOKE_RETURNED\nPS_NATIVE_ENGINE_OK\nPS_INIT_RUNSPACE_DISPOSE_AFTER\n",
        stderr="PS_PIPELINE_INCOMPLETE\n",
    )

    summary = summarize_process(result)
    assert summary["exit_code"] == 2
    assert summary["markers"]["PS_INIT_PIPELINE_INVOKE_RETURNED"] is True
    assert summary["markers"]["PS_NATIVE_CHILD_OK"] is False
    assert summary["markers"]["PS_INIT_DONE"] is False
