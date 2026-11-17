"""Synthetic-only file and loopback controls for the LPAC escape payload."""

from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native/windows/MewCode.EscapePayload.cs"
CSC = Path("C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe")


@pytest.fixture(scope="module")
def payload():
    if sys.platform != "win32" or not CSC.is_file():
        pytest.skip("Windows .NET Framework compiler is unavailable")
    with tempfile.TemporaryDirectory(prefix="mewcode-escape-build-", dir=ROOT / ".tmp") as directory:
        executable = Path(directory) / "MewCode.EscapePayload.exe"
        compiled = subprocess.run(
            [str(CSC), "/nologo", "/platform:x64", "/target:exe",
             "/out:" + str(executable), str(SOURCE)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        )
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
        assert executable.is_file()
        yield executable


def _run(executable: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=5, check=False,
    )


def test_source_is_fixed_to_synthetic_paths_and_loopback_targets() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    for fixed in (
        '"input.txt"', '"junction", "junction-escaped.txt"',
        '".mewcode", "permissions.grants.json"',
        '".mewcode", "rules", "host.rules"',
        "IPAddress.Loopback", "IPAddress.IPv6Loopback",
        'mode == "tcp4"', 'mode == "tcp6"', 'mode == "udp4"', 'mode == "udp6"',
        "SocketError.AccessDenied", "NetworkTimeoutMs = 2000",
    ):
        assert fixed in source
    for forbidden in ("Dns.GetHost", "HttpClient", "WebRequest", "Process.Start", "Environment.Exit"):
        assert forbidden not in source
    assert "ex.Message" not in source


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("network", "tcp4", "0", "nonce_123"),
        ("network", "udp6", "65536", "nonce_123"),
        ("network", "tcp4", "+5", "nonce_123"),
        ("network", "tcp4", "5000", "bad#nonce"),
        ("network", "tcp4", "5000", "nonce_123", "127.0.0.2"),
        ("network", "tcp4", "5000", "x" * 129),
        ("network", "tcp7", "5000", "nonce_123"),
        ("files", "C:\\Windows", "C:\\Temp", "nonce_123"),
        ("files", "..\\workspace", "..\\outside", "nonce_123"),
    ],
)
def test_rejects_unfixed_parameters_before_access(payload: Path, args: tuple[str, ...]) -> None:
    completed = _run(payload, *args)
    assert completed.returncode == 2
    assert completed.stdout.splitlines() == ["ESCAPE_INVALID_ARGUMENTS"]


def test_files_host_control_reports_every_allowed_access_without_contents(payload: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mewcode-lpac-probe-", dir=ROOT / ".tmp") as directory:
        fixture = Path(directory)
        workspace = fixture / "workspace"
        outside = fixture / "outside"
        (workspace / "junction").mkdir(parents=True)
        (workspace / ".mewcode" / "rules").mkdir(parents=True)
        outside.mkdir()
        (workspace / "input.txt").write_text("SYNTHETIC_INPUT", encoding="utf-8")
        authority_text = "SYNTHETIC_AUTHORITY_CONTENT"
        (workspace / ".mewcode" / "permissions.grants.json").write_text(authority_text, encoding="utf-8")
        (workspace / ".mewcode" / "rules" / "host.rules").write_text(authority_text, encoding="utf-8")
        nonce = secrets.token_hex(8)
        completed = _run(payload, "files", str(workspace), str(outside), nonce)

    assert completed.returncode == 2
    lines = set(completed.stdout.splitlines())
    assert "FILES_" + nonce in lines
    assert "INPUT_OK" in lines
    for marker in (
        "JUNCTION_WRITE_ALLOWED", "AUTH_GRANTS_READ_ALLOWED", "AUTH_GRANTS_WRITE_ALLOWED",
        "AUTH_RULES_READ_ALLOWED", "AUTH_RULES_WRITE_ALLOWED", "FILES_DONE",
    ):
        assert marker in lines
    assert authority_text not in completed.stdout


@pytest.mark.parametrize(
    "mode,family,kind,host",
    [
        ("tcp4", socket.AF_INET, socket.SOCK_STREAM, "127.0.0.1"),
        ("tcp6", socket.AF_INET6, socket.SOCK_STREAM, "::1"),
        ("udp4", socket.AF_INET, socket.SOCK_DGRAM, "127.0.0.1"),
        ("udp6", socket.AF_INET6, socket.SOCK_DGRAM, "::1"),
    ],
)
def test_network_host_control_sends_only_nonce_to_loopback(
    payload: Path, mode: str, family: int, kind: int, host: str,
) -> None:
    nonce = secrets.token_hex(12)
    with socket.socket(family, kind) as listener:
        try:
            listener.bind((host, 0))
        except OSError as exc:
            pytest.skip(f"Loopback family unavailable: {type(exc).__name__}")
        listener.settimeout(2)
        if kind == socket.SOCK_STREAM:
            listener.listen(1)
        port = listener.getsockname()[1]
        completed = _run(payload, "network", mode, str(port), nonce)
        if kind == socket.SOCK_STREAM:
            with listener.accept()[0] as connection:
                connection.settimeout(2)
                received = connection.recv(128)
        else:
            received = listener.recvfrom(128)[0]

    assert completed.returncode == 2  # ALLOWED is a finding, not a boundary pass.
    assert completed.stdout.splitlines() == ["NETWORK_" + mode.upper() + "_ALLOWED"]
    assert received == nonce.encode("ascii")
