from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_MARKER = ".mewcode-synthetic-fixture"
FIXTURE_MARKER_CONTENT = b"mewcode-synthetic-only-v1\n"


@pytest.fixture(scope="module")
def helper_executable() -> Path:
    if os.name != "nt":
        pytest.skip("native AppContainer helper validation runs only on Windows")
    configured = os.environ.get("MEWCODE_APPCONTAINER_PROBE_EXE")
    if not configured:
        pytest.skip("set MEWCODE_APPCONTAINER_PROBE_EXE to opt in")
    helper = Path(configured)
    if not helper.is_absolute():
        pytest.fail("MEWCODE_APPCONTAINER_PROBE_EXE must be an absolute path")
    if not helper.is_file():
        pytest.fail("MEWCODE_APPCONTAINER_PROBE_EXE must point to an existing helper")
    return helper.resolve()


@pytest.fixture
def synthetic_fixture():
    scratch = REPO_ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mewcode-lpac-probe-", dir=scratch) as root_text:
        with tempfile.TemporaryDirectory(prefix="mewcode-lpac-probe-outside-", dir=scratch) as outside_text:
            root = Path(root_text)
            outside = Path(outside_text)
            (root / FIXTURE_MARKER).write_bytes(FIXTURE_MARKER_CONTENT)
            workspace = root / "workspace"
            runtime_temp = workspace / "temp"
            runtime_temp.mkdir(parents=True)
            executable = workspace / "dummy.exe"
            executable.write_bytes(b"synthetic executable only")

            system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
            system32 = Path(system_root) / "System32"
            environment = {
                "SystemRoot": system_root,
                "WINDIR": system_root,
                "SystemDrive": Path(system_root).drive,
                "COMSPEC": str(system32 / "cmd.exe"),
                "PATH": str(system32),
                "TEMP": str(runtime_temp),
                "TMP": str(runtime_temp),
            }
            yield {
                "root": root,
                "outside": outside,
                "workspace": workspace,
                "runtime_temp": runtime_temp,
                "executable": executable,
                "environment": environment,
            }


def _request(fixture: dict, **overrides) -> dict:
    request = {
        "fixture_root": str(fixture["root"]),
        "executable": str(fixture["executable"]),
        "arguments": ["synthetic-probe-argument"],
        "cwd": str(fixture["workspace"]),
        "timeout_ms": 20_000,
        "environment": fixture["environment"],
        "read_paths": [str(fixture["workspace"])],
        "write_paths": [str(fixture["workspace"])],
        "lpac": True,
    }
    request.update(overrides)
    return request


def _invoke_invalid_request(helper: Path, fixture: dict, request: dict) -> dict:
    request_path = fixture["root"] / f"invalid-{uuid.uuid4().hex}.request.json"
    request_path.write_text(json.dumps(request, ensure_ascii=True), encoding="utf-8")
    completed = subprocess.run(
        [str(helper), str(request_path)],
        capture_output=True,
        timeout=30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        receipt = json.loads(completed.stdout.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        pytest.fail(
            "helper did not return a JSON receipt; "
            f"exit={completed.returncode}, stdout={completed.stdout!r}, "
            f"stderr={completed.stderr!r}, decode_error={exc}"
        )
    assert completed.returncode != 0
    assert receipt.get("pid") is None
    assert receipt.get("profile_name") is None
    assert receipt.get("appcontainer_verified") is False
    assert receipt.get("error")
    return receipt


def _make_hardlink_or_skip(source: Path, alias: Path) -> None:
    try:
        os.link(source, alias)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"cannot create a synthetic hard link here: {exc}")
    if source.stat().st_nlink <= 1:
        pytest.skip("filesystem did not report multiple links for the synthetic hard link")


def test_execute_paths_helper_contract_is_optional_and_exact_file_only():
    source = (REPO_ROOT / "native" / "windows" / "MewCode.AppContainerProbe.cs").read_text(
        encoding="utf-8"
    )

    assert "public string[] execute_paths { get; set; }" in source
    assert "if (request.execute_paths != null)" in source
    assert 'AddRequestedExecuteGrants(request.execute_paths, "execute_paths", fixtureRoot, grants)' in source
    assert 'ValidateExistingPath(paths[i], label + "[" + i.ToString() + "]", false)' in source
    assert "RejectHardLinkedFile(path, label + \"[\" + i.ToString() + \"]\")" in source
    assert "AddGrant(grants, path, false, true, false, true, label)" in source


def test_rejects_execute_path_outside_fixture_before_launch(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture
    outside_file = fixture["outside"] / "synthetic-external.dll"
    outside_file.write_bytes(b"synthetic only")

    receipt = _invoke_invalid_request(
        helper_executable,
        fixture,
        _request(fixture, execute_paths=[str(outside_file)]),
    )

    assert "execute_paths[0]" in receipt["error"]
    assert "strictly beneath fixture_root" in receipt["error"]


def test_rejects_execute_path_directory_before_launch(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture

    receipt = _invoke_invalid_request(
        helper_executable,
        fixture,
        _request(fixture, execute_paths=[str(fixture["workspace"])]),
    )

    assert "execute_paths[0] must be a file" in receipt["error"]


def test_rejects_reparse_execute_path_before_launch_when_symlink_creation_is_available(
    helper_executable, synthetic_fixture
):
    fixture = synthetic_fixture
    target = fixture["outside"] / "synthetic-target.dll"
    target.write_bytes(b"synthetic only")
    link = fixture["workspace"] / "synthetic-execute-link.dll"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"cannot create a synthetic file symlink here: {exc}")

    receipt = _invoke_invalid_request(
        helper_executable,
        fixture,
        _request(fixture, execute_paths=[str(link)]),
    )

    assert "reparse point" in receipt["error"].lower()


def test_rejects_hardlinked_execute_path_before_launch(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture
    outside_file = fixture["outside"] / "synthetic-source.dll"
    outside_file.write_bytes(b"synthetic only")
    alias = fixture["workspace"] / "synthetic-hardlink.dll"
    _make_hardlink_or_skip(outside_file, alias)

    receipt = _invoke_invalid_request(
        helper_executable,
        fixture,
        _request(fixture, execute_paths=[str(alias)]),
    )

    assert "execute_paths[0]" in receipt["error"]
    assert "numberoflinks > 1" in receipt["error"].lower()


def test_rejects_fixture_root_as_a_grant_before_launch(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture
    request = _request(fixture, read_paths=[str(fixture["root"])])

    receipt = _invoke_invalid_request(helper_executable, fixture, request)

    assert "fixture_root itself" in receipt["error"]


def test_rejects_synthetic_sibling_outside_fixture_grant(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture
    outside_file = fixture["outside"] / "synthetic-outside.txt"
    outside_file.write_text("synthetic outside data only", encoding="utf-8")
    request = _request(
        fixture,
        read_paths=[str(fixture["workspace"]), str(outside_file)],
    )

    receipt = _invoke_invalid_request(helper_executable, fixture, request)

    assert "strictly beneath fixture_root" in receipt["error"]


def test_rejects_reparse_point_grant_when_symlink_creation_is_available(
    helper_executable, synthetic_fixture
):
    fixture = synthetic_fixture
    outside = fixture["outside"]
    (outside / "synthetic-target").mkdir()
    link = fixture["workspace"] / "synthetic-link"
    try:
        link.symlink_to(outside / "synthetic-target", target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"cannot create a synthetic directory symlink here: {exc}")
    request = _request(
        fixture,
        read_paths=[str(fixture["workspace"]), str(link)],
    )

    receipt = _invoke_invalid_request(helper_executable, fixture, request)

    assert "reparse point" in receipt["error"].lower()


def test_rejects_hardlinked_synthetic_marker(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture
    marker = fixture["root"] / FIXTURE_MARKER
    _make_hardlink_or_skip(marker, fixture["outside"] / "marker-alias")

    receipt = _invoke_invalid_request(
        helper_executable,
        fixture,
        _request(fixture),
    )

    assert "fixture marker" in receipt["error"].lower()
    assert "numberoflinks > 1" in receipt["error"].lower()


def test_rejects_hardlinked_granted_file(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture
    granted = fixture["workspace"] / "synthetic-readable.txt"
    granted.write_text("synthetic only", encoding="utf-8")
    _make_hardlink_or_skip(granted, fixture["outside"] / "readable-alias.txt")
    request = _request(
        fixture,
        read_paths=[str(fixture["workspace"]), str(granted)],
    )

    receipt = _invoke_invalid_request(helper_executable, fixture, request)

    assert "read_paths[1]" in receipt["error"]
    assert "numberoflinks > 1" in receipt["error"].lower()


def test_rejects_hardlinked_fixture_executable(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture
    _make_hardlink_or_skip(
        fixture["executable"],
        fixture["outside"] / "dummy-alias.exe",
    )

    receipt = _invoke_invalid_request(
        helper_executable,
        fixture,
        _request(fixture),
    )

    assert "fixture executable" in receipt["error"].lower()
    assert "numberoflinks > 1" in receipt["error"].lower()


def test_wrong_marker_rejects_even_trusted_hardlinked_system_executable(
    helper_executable, synthetic_fixture
):
    fixture = synthetic_fixture
    system_root = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows")
    candidates = [
        system_root / "System32" / "cmd.exe",
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
    ]
    linked_system_executable = next(
        (candidate for candidate in candidates if candidate.is_file() and candidate.stat().st_nlink > 1),
        None,
    )
    if linked_system_executable is None:
        pytest.skip("no trusted System32 probe executable has multiple links on this Windows image")

    # Marker validation precedes executable validation. This proves the fixture
    # guard cannot be bypassed by naming a trusted executable; actual trusted
    # executable compatibility is established separately by the live CMD probe.
    (fixture["root"] / FIXTURE_MARKER).write_bytes(b"x" * len(FIXTURE_MARKER_CONTENT))
    receipt = _invoke_invalid_request(
        helper_executable,
        fixture,
        _request(fixture, executable=str(linked_system_executable)),
    )

    assert "fixture marker content" in receipt["error"].lower()


def test_rejects_wrong_synthetic_marker_before_launch(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture
    (fixture["root"] / FIXTURE_MARKER).write_bytes(b"not the synthetic-only marker\n")

    receipt = _invoke_invalid_request(
        helper_executable,
        fixture,
        _request(fixture),
    )

    assert "fixture marker" in receipt["error"].lower()


def test_rejects_timeout_above_limit_before_launch(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture

    receipt = _invoke_invalid_request(
        helper_executable,
        fixture,
        _request(fixture, timeout_ms=60_001),
    )

    assert "timeout_ms" in receipt["error"]


def test_rejects_synthetic_untrusted_executable_outside_fixture(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture
    outside_executable = fixture["outside"] / "dummy.exe"
    outside_executable.write_bytes(b"synthetic non-system executable only")

    receipt = _invoke_invalid_request(
        helper_executable,
        fixture,
        _request(fixture, executable=str(outside_executable)),
    )

    assert "executable must be inside fixture_root" in receipt["error"]


def test_rejects_nul_argument_before_launch(helper_executable, synthetic_fixture):
    fixture = synthetic_fixture

    receipt = _invoke_invalid_request(
        helper_executable,
        fixture,
        _request(fixture, arguments=["synthetic\x00invalid"]),
    )

    assert "without NUL" in receipt["error"]


def test_registry_read_is_rejected_for_regular_candidate_before_launch(helper_executable, synthetic_fixture):
    receipt = _invoke_invalid_request(
        helper_executable, synthetic_fixture,
        _request(synthetic_fixture, lpac=False, runtime_registry_read=True),
    )
    assert "registry" in receipt["error"].lower()
    assert "lpac" in receipt["error"].lower()


@pytest.mark.parametrize("lpac,registry", [(False, False), (False, True), (True, False)])
def test_com_requires_lpac_and_registry_before_launch(helper_executable, synthetic_fixture, lpac, registry):
    receipt = _invoke_invalid_request(
        helper_executable, synthetic_fixture,
        _request(synthetic_fixture, lpac=lpac, runtime_registry_read=registry, runtime_com=True),
    )
    assert "registry" in receipt["error"].lower() or "runtime_com" in receipt["error"].lower()


@pytest.mark.parametrize("lpac,registry", [(False, False), (False, True), (True, False)])
def test_instrumentation_requires_lpac_and_registry_before_launch(helper_executable, synthetic_fixture, lpac, registry):
    receipt = _invoke_invalid_request(
        helper_executable, synthetic_fixture,
        _request(synthetic_fixture, lpac=lpac, runtime_registry_read=registry, runtime_instrumentation=True),
    )
    assert "registry" in receipt["error"].lower() or "instrumentation" in receipt["error"].lower()
