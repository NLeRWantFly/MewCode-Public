"""Opt-in real Windows srt escape probes using only synthetic targets.

Run only after an explicit host-install authorization. A passing smoke test is
not enough: each test launches a fresh srt session and records actual effects.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from mewcode.sandbox.srt_windows import build_srt_pilot_settings, srt_pilot_argv


pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("MEWCODE_TEST_SRT_SANDBOX") != "1",
    reason="requires explicit Windows srt host installation and opt-in",
)


def _quote_ps(path: Path) -> str:
    return str(path).replace("'", "''")


def _powershell(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"


def _run_srt(root: Path, profile: Path, output: Path, script: str) -> subprocess.CompletedProcess[str]:
    settings_path = root.parent / "srt-settings.json"
    synthetic_env = {"USERPROFILE": str(profile)}
    settings_path.write_text(json.dumps(build_srt_pilot_settings(
        root, read_directories=[root], write_directories=[output], env=synthetic_env,
    )), encoding="utf-8")
    argv = srt_pilot_argv(
        _powershell(script), work_dir=root, settings_path=settings_path,
        node_exe=os.environ["MEWCODE_SRT_NODE_EXE"], srt_cli=os.environ["MEWCODE_SRT_CLI"],
        read_directories=[root], write_directories=[output], env=synthetic_env,
    )
    raw = subprocess.run(argv, cwd=root, capture_output=True, timeout=45)
    # Windows PowerShell and srt may emit different code pages to the same
    # pipe. Decode after completion so a diagnostic byte cannot kill a reader
    # thread and turn stderr into None.
    result = subprocess.CompletedProcess(
        raw.args, raw.returncode,
        (raw.stdout or b"").decode("utf-8", errors="replace"),
        (raw.stderr or b"").decode("utf-8", errors="replace"),
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    return result


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "workspace"
    output = root / "output"
    profile = tmp_path / "synthetic-profile"
    outside = tmp_path / "synthetic-outside"
    for path in (output, profile, outside):
        path.mkdir(parents=True)
    return root, output, profile, outside


def _allow_authenticated_users_modify(path: Path) -> None:
    # This reproduces a common Windows ACL that can defeat additive allowWrite.
    result = subprocess.run(
        ["icacls.exe", str(path), "/grant", "*S-1-5-11:(OI)(CI)M"],
        capture_output=True, text=True, errors="replace", timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _revoke_synthetic_modify(path: Path) -> None:
    result = subprocess.run(
        ["icacls.exe", str(path), "/remove:g", "*S-1-5-11"],
        capture_output=True, text=True, errors="replace", timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_srt_allow_write_and_deny_permissive_outside(tmp_path: Path) -> None:
    root, output, profile, outside = _fixture(tmp_path)
    allowed = output / "allowed.txt"
    escaped = outside / "escaped.txt"
    _allow_authenticated_users_modify(outside)
    try:
        allow_script = (
            f"try {{ [IO.File]::WriteAllText('{_quote_ps(allowed)}','synthetic'); "
            "Write-Output WRITE_ALLOWED } catch { Write-Output WRITE_DENIED }"
        )
        allowed_result = _run_srt(root, profile, output, allow_script)
        assert "WRITE_ALLOWED" in allowed_result.stdout, allowed_result.stdout
        assert allowed.exists(), "sandbox reported a write but no output file exists"

        escape_script = (
            f"try {{ [IO.File]::WriteAllText('{_quote_ps(escaped)}','synthetic'); "
            "Write-Output OUTSIDE_WRITE_ALLOWED } catch { Write-Output OUTSIDE_WRITE_DENIED }"
        )
        result = _run_srt(root, profile, output, escape_script)
        findings: list[str] = []
        if "OUTSIDE_WRITE_DENIED" not in result.stdout or "OUTSIDE_WRITE_ALLOWED" in result.stdout:
            findings.append("permissive outside directory was writable")
        if escaped.exists():
            findings.append("sandbox created an out-of-scope file")
        try:
            if allowed.read_text(encoding="utf-8") != "synthetic":
                findings.append("host cannot read the expected output content")
        except OSError:
            findings.append("host cannot read the sandbox-created output file")
        assert not findings, "; ".join(findings) + "\n" + result.stdout
    finally:
        _revoke_synthetic_modify(outside)


def test_real_srt_denies_synthetic_profile_read(tmp_path: Path) -> None:
    root, output, profile, _ = _fixture(tmp_path)
    secret = profile / "synthetic-secret.txt"
    secret.write_text("SYNTHETIC_SECRET_ONLY", encoding="utf-8")
    script = (
        f"try {{ $s=[IO.File]::ReadAllText('{_quote_ps(secret)}'); "
        "Write-Output READ_ALLOWED } catch { Write-Output READ_DENIED }"
    )
    result = _run_srt(root, profile, output, script)
    assert "READ_DENIED" in result.stdout, result.stdout
    assert "READ_ALLOWED" not in result.stdout, result.stdout
    assert "SYNTHETIC_SECRET_ONLY" not in result.stdout + result.stderr


def test_real_srt_denies_non_proxy_loopback(tmp_path: Path) -> None:
    root, output, profile, _ = _fixture(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        assert not 60080 <= port <= 60089
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass  # Host-side reachability control.
        script = (
            "$c=[Net.Sockets.TcpClient]::new(); "
            f"try {{ $t=$c.ConnectAsync('127.0.0.1',{port}); "
            "if ($t.Wait(2000) -and $c.Connected) { Write-Output LOOPBACK_ALLOWED } "
            "else { Write-Output LOOPBACK_DENIED } } "
            "catch { Write-Output LOOPBACK_DENIED } finally { $c.Close() }"
        )
        result = _run_srt(root, profile, output, script)
    assert "LOOPBACK_DENIED" in result.stdout, result.stdout
    assert "LOOPBACK_ALLOWED" not in result.stdout, result.stdout


def test_real_srt_denies_junction_escape(tmp_path: Path) -> None:
    root, output, profile, outside = _fixture(tmp_path)
    junction = output / "outside-link"
    escaped = outside / "junction-escaped.txt"
    _allow_authenticated_users_modify(outside)
    try:
        created = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True, text=True, errors="replace", timeout=10,
        )
        assert created.returncode == 0, created.stdout + created.stderr
        script = (
            f"try {{ [IO.File]::WriteAllText('{_quote_ps(junction / escaped.name)}','synthetic'); "
            "Write-Output JUNCTION_WRITE_ALLOWED } catch { Write-Output JUNCTION_WRITE_DENIED }"
        )
        result = _run_srt(root, profile, output, script)
        assert "JUNCTION_WRITE_DENIED" in result.stdout, result.stdout
        assert "JUNCTION_WRITE_ALLOWED" not in result.stdout, result.stdout
        assert not escaped.exists(), "sandbox wrote through an output-directory junction"
    finally:
        _revoke_synthetic_modify(outside)
