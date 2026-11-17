"""Explicit, synthetic-only Windows native boundary experiment.

Never called by the agent tool runtime. This creates a disposable per-user
AppContainer through the reviewed helper; it never grants production access.
Fixtures/reports are retained for inspection rather than recursively deleted.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def powershell_arguments(script: str, *, mta: bool = False) -> list[str]:
    # Preserve non-ASCII paths and quotes independently of console code pages.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["-NoLogo", "-NoProfile", "-NonInteractive"] + (["-Mta"] if mta else []) + ["-EncodedCommand", encoded]


def process_exited(pid: int) -> bool:
    """Access denied is inconclusive, never evidence of process exit."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 87  # nonexistent PID, not access denied
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel.CloseHandle(handle)


def assess_smoke(report: dict, nonce: str) -> dict:
    """Narrow diagnostic verdict; never a production attestation."""
    cases = {item["name"]: item for item in report.get("cases", [])}
    expected_capabilities = []
    if report.get("runtime_registry_read"):
        expected_capabilities.append("registryRead")
    if report.get("runtime_instrumentation"):
        expected_capabilities.append("lpacInstrumentation")
    if report.get("runtime_com"):
        expected_capabilities.append("lpacCom")

    def success(name: str, markers: tuple[str, ...], *, expected_exit: int = 0) -> bool:
        case = cases.get(name, {})
        receipt = case.get("receipt", {})
        lines = str(receipt.get("stdout", "")).splitlines()
        return (
            case.get("helper_exit_code") == expected_exit and receipt.get("exit_code") == expected_exit
            and receipt.get("capabilities") == expected_capabilities
            and not receipt.get("error") and receipt.get("timed_out") is False
            and receipt.get("stdout_truncated") is False and receipt.get("stderr_truncated") is False
            and (not report.get("lpac") or receipt.get("lpac_verified") is True)
            and all(receipt.get(field) is True for field in (
                "job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok", "appcontainer_verified",
            ))
            and all(lines.count(marker) == 1 for marker in markers)
        )

    observed = report.get("observed", {})
    native_markers = ("EXE_" + nonce, "ENV_CLEAN", "INPUT_OK", "OUTPUT_OK", "OUTSIDE_READ_DENIED",
                      "SECRET_READ_DENIED", "JUNCTION_READ_DENIED", "OUTSIDE_WRITE_DENIED")
    network_denied = success("exe_boundary", native_markers + ("NETWORK_DENIED",))
    # A firewall may silently drop, not return AccessDenied. Only count that
    # bounded no-delivery observation with an exact-payload host positive control.
    network_dropped = (
        report.get("host_control", {}).get("same_payload_network_reachable") is True
        and observed.get("network_child_payload_received") is False
        and success("exe_boundary", native_markers + ("NETWORK_UNKNOWN_TIMEOUT",), expected_exit=2)
    )
    checks = {
        "cmd_native_compatible": success("cmd_smoke", ("CMD_" + nonce,)),
        "powershell_boundary_smoke": success("powershell_boundary", (
            "POWERSHELL_" + nonce, "ENV_CLEAN", "INPUT_OK", "OUTPUT_OK", "OUTSIDE_READ_DENIED",
            "SECRET_READ_DENIED", "OUTSIDE_WRITE_DENIED",
        )),
        "exe_boundary_smoke": network_denied or network_dropped,
        "loopback_host_observation": (network_denied or network_dropped)
            and observed.get("network_child_payload_received") is False,
        "outside_write_host_observation": success("powershell_boundary", ("POWERSHELL_" + nonce, "OUTSIDE_WRITE_DENIED"))
            and observed.get("outside_write_exists") is False,
        "host_can_read_output": observed.get("output_readable_by_host") is True,
        "host_can_read_exe_output": observed.get("exe_output_readable_by_host") is True,
        "fixture_acl_cleanup": report.get("fixture_acl_cleanup_ok") is True,
    }
    if report.get("alias_workspace_drive"):
        checks["fixture_drive_alias_cleanup"] = report.get("drive_alias_cleanup_ok") is True
    lifecycle = {}
    for name, expect_timeout in (("root_exit_descendants", False), ("timeout_descendants", True)):
        case = cases.get(name, {})
        receipt = case.get("receipt", {})
        lifecycle[name] = (
            case.get("descendant_exit_observed") is True
            and receipt.get("timed_out") is expect_timeout
            and receipt.get("appcontainer_verified") is True
            and receipt.get("capabilities") == expected_capabilities
            and (not report.get("lpac") or receipt.get("lpac_verified") is True)
            and all(receipt.get(key) is True for key in (
                "job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok"))
            and receipt.get("stdout_truncated") is False
            and receipt.get("stderr_truncated") is False
            and (receipt.get("error") == "child process exceeded timeout_ms" if expect_timeout
                 else not receipt.get("error") and receipt.get("exit_code") == 0
                 and case.get("helper_exit_code") == 0)
        )
    cancel_case = cases.get("cancellation_descendants", {})
    cancel_receipt = cancel_case.get("receipt", {})
    lifecycle["cancellation_descendants"] = (
        cancel_case.get("cancel_signal_sent") is True
        and cancel_case.get("descendant_exit_observed") is True
        and cancel_case.get("helper_exit_code") == 1
        and cancel_receipt.get("cancelled") is True
        and cancel_receipt.get("timed_out") is False
        and not cancel_receipt.get("error")
        and cancel_receipt.get("capabilities") == expected_capabilities
        and (not report.get("lpac") or cancel_receipt.get("lpac_verified") is True)
        and all(cancel_receipt.get(key) is True for key in (
            "job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok", "appcontainer_verified"))
        and cancel_receipt.get("stdout_truncated") is False
        and cancel_receipt.get("stderr_truncated") is False
    )
    missing_checks = ["junction_write_escape", "ipv6_udp_egress", "external_egress",
                      "authority_paths", "runtime_dependencies",
                      "post_launch_secret_creation", "path_replacement_races"]
    missing_checks.extend(name for name, passed in lifecycle.items() if not passed)
    if not (network_denied or network_dropped) or not report.get("host_control", {}).get("junction_target_readable"):
        missing_checks.append("junction_read_escape")
    # A failed acceptance check is not itself evidence of an escaped write:
    # PowerShell may never have reached the probe. Keep observation and proof
    # separate, without changing the fail-closed checks above.
    outside_exists = observed.get("outside_write_exists")
    outside_write_observation = (
        "write_observed" if outside_exists is True else
        "denial_verified" if checks["outside_write_host_observation"] else
        "no_write_observed_probe_incomplete" if outside_exists is False else
        "observation_unavailable"
    )
    return {"checks": checks, "smoke_passed": all(checks.values()),
            "outside_write_observation": outside_write_observation,
            "network_observation": ("explicit_access_denied" if network_denied else
                                    "bounded_no_delivery_with_same_payload_control" if network_dropped else "inconclusive"),
            "lifecycle_checks": lifecycle,
            "production_authorized": False,
            "missing_checks": missing_checks}


async def experiment(helper: Path, *, lpac: bool, runtime_registry_read: bool = False,
                     runtime_instrumentation: bool = False, powershell_timeout_ms: int = 10000,
                     waitchain_helper: Path | None = None, writable_runtime_profile: bool = False,
                     diagnose_powershell_init: bool = False, powershell_mta: bool = False,
                     diagnose_cancellation: bool = False, alias_workspace_drive: bool = False,
                     managed_stack_helper: Path | None = None,
                     diagnose_dotnet_threadpool: bool = False, runtime_com: bool = False) -> dict:
    from mewcode.sandbox.appcontainer_policy import build_probe_request
    from scripts.native_probe_resources import ProbeCancelEvent, ProbeDriveAlias

    scratch = ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix="mewcode-lpac-probe-", dir=scratch))
    (fixture / ".mewcode-synthetic-fixture").write_bytes(b"mewcode-synthetic-only-v1\n")
    workspace, outside = fixture / "workspace", fixture / "outside"
    for directory in (workspace, workspace / "temp", outside):
        directory.mkdir()
    for directory in (fixture / "profile/AppData/Local", fixture / "profile/AppData/Roaming"):
        directory.mkdir(parents=True)
    (workspace / "input.txt").write_text("SYNTHETIC_INPUT", encoding="utf-8")
    for filename in ("ordinary.txt", ".env"):
        (outside / filename).write_text("SYNTHETIC_ONLY", encoding="utf-8")
    nonce = uuid.uuid4().hex
    received: list[bytes] = []
    control_seen = asyncio.Event()
    payload_control_seen = asyncio.Event()
    handlers: set[asyncio.Task] = set()

    async def accept(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            data = await asyncio.wait_for(reader.read(128), 3)
            received.append(data)
            if data == ("host_" + nonce).encode():
                control_seen.set()
            if data == ("payload_host_" + nonce).encode():
                payload_control_seen.set()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    report = {
        "schema": "mewcode-native-experiment-v1", "lpac": lpac,
        "runtime_registry_read": runtime_registry_read,
        "runtime_instrumentation": runtime_instrumentation,
        "runtime_com": runtime_com,
        "powershell_timeout_ms": powershell_timeout_ms,
        "writable_runtime_profile": writable_runtime_profile,
        "diagnose_powershell_init": diagnose_powershell_init,
        "powershell_mta": powershell_mta,
        "diagnose_cancellation": diagnose_cancellation,
        "alias_workspace_drive": alias_workspace_drive,
        "diagnose_dotnet_threadpool": diagnose_dotnet_threadpool,
        "fixture": str(fixture), "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "production_authorized": False, "cases": [],
    }
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    payload = workspace / "synthetic-payload.exe"
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    compiled = subprocess.run(
        [str(compiler), "/nologo", "/out:" + str(payload),
         str(ROOT / "native/windows/MewCode.SyntheticPayload.cs")],
        capture_output=True, timeout=30,
    )
    if compiled.returncode:
        raise RuntimeError("Synthetic payload compilation failed: " + compiled.stdout.decode(errors="replace"))
    report["payload_sha256"] = hashlib.sha256(payload.read_bytes()).hexdigest()
    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    drive_alias = ProbeDriveAlias(fixture) if alias_workspace_drive else None

    async def run_case(name: str, arguments: list[str], exe: Path = executable, *, timeout_ms: int = 10000,
                       cancel_after_ms: int | None = None):
        profile_reads = [fixture / "profile", fixture / "profile/AppData", fixture / "profile/AppData/Local",
                         fixture / "profile/AppData/Roaming"] if writable_runtime_profile else []
        profile_writes = profile_reads[-2:] if writable_runtime_profile else []
        request = build_probe_request(
            fixture_root=fixture, executable=exe, arguments=arguments,
            cwd=workspace, read_paths=[workspace, workspace / "input.txt", workspace / "temp"] + profile_reads,
            write_paths=[workspace, workspace / "temp"] + profile_writes,
            timeout_ms=timeout_ms, lpac=lpac, runtime_registry_read=runtime_registry_read,
            runtime_instrumentation=runtime_instrumentation,
            runtime_com=runtime_com,
        )
        cancel_event = ProbeCancelEvent() if cancel_after_ms is not None else None
        if cancel_event:
            request["cancel_event_name"] = cancel_event.name
        request_path = fixture / (name + ".request.json")
        try:
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            process = await asyncio.create_subprocess_exec(
                str(helper), str(request_path), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except BaseException:
            if cancel_event:
                cancel_event.close()
            raise
        diagnostics: list[dict] = []
        managed_stacks: list[dict] = []
        cancel_signal_sent = False

        async def send_cancel():
            nonlocal cancel_signal_sent
            await asyncio.sleep(cancel_after_ms / 1000)
            if process.returncode is None:
                cancel_event.signal()
                cancel_signal_sent = True

        cancel_timer = asyncio.create_task(send_cancel()) if cancel_event else None

        async def read_host_diagnostics() -> bytes:
            captured = bytearray()
            while line := await process.stderr.readline():
                captured.extend(line[:max(0, 32768 - len(captured))])
                match = re.fullmatch(rb"MEWCODE_CHILD_PID=(\d+)\r?\n", line)
                if not match:
                    continue
                if managed_stack_helper and name in {
                    "powershell_boundary", "powershell_init_diagnostic", "powershell_drive_diagnostic",
                }:
                    await asyncio.sleep(2)
                    observer = await asyncio.create_subprocess_exec(
                        str(managed_stack_helper), "--pid", match.group(1).decode("ascii"),
                        "--fixture", str(fixture), stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    try:
                        out, err = await asyncio.wait_for(observer.communicate(), 10)
                        try:
                            data = json.loads(out.decode("utf-8-sig"))
                        except (UnicodeError, ValueError):
                            data = {"error": "invalid_managed_stack_receipt"}
                        managed_stacks.append({"exit_code": observer.returncode, "receipt": data,
                                               "stderr": err.decode(errors="replace")[:4000]})
                    except asyncio.TimeoutError:
                        managed_stacks.append({"error": "managed_stack_observer_deadline"})
                    finally:
                        if observer.returncode is None:
                            observer.kill()
                            await observer.wait()
                if not waitchain_helper or name != "powershell_boundary":
                    continue
                await asyncio.sleep(2)
                observer = await asyncio.create_subprocess_exec(
                    str(waitchain_helper), "--pid", match.group(1).decode("ascii"),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    out, err = await asyncio.wait_for(observer.communicate(), 22)
                    try:
                        data = json.loads(out.decode("utf-8-sig"))
                    except (UnicodeError, ValueError):
                        data = {"error": "invalid_waitchain_receipt"}
                    diagnostics.append({"exit_code": observer.returncode, "receipt": data,
                                        "stderr": err.decode(errors="replace")[:4000]})
                except asyncio.TimeoutError:
                    diagnostics.append({"error": "waitchain_observer_deadline"})
                finally:
                    # Cancellation of the outer capture must not orphan the
                    # diagnostic process. Its own Job contains only observers.
                    if observer.returncode is None:
                        observer.kill()
                        await observer.wait()
            return bytes(captured)

        async def capture_helper() -> tuple[bytes, bytes]:
            out, err = await asyncio.gather(process.stdout.read(), read_host_diagnostics())
            await process.wait()
            return out, err

        try:
            out, err = await asyncio.wait_for(capture_helper(), max(30, timeout_ms / 1000 + 25))
            try:
                receipt = json.loads(out.decode("utf-8-sig"))
            except (UnicodeError, ValueError):
                receipt = {"error": "invalid_helper_receipt", "stdout": out.decode(errors="replace")[:4000]}
            item = {"name": name, "helper_exit_code": process.returncode,
                    "receipt": receipt, "helper_stderr": err.decode(errors="replace")[:4000],
                    "waitchain": diagnostics, "managed_stacks": managed_stacks}
        except asyncio.TimeoutError:
            item = {"name": name, "error": "helper_timeout_cleanup_unconfirmed"}
        finally:
            try:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            finally:
                if cancel_timer:
                    cancel_timer.cancel()
                    await asyncio.gather(cancel_timer, return_exceptions=True)
                if cancel_event:
                    cancel_event.close()
        if cancel_event:
            item["cancel_signal_sent"] = cancel_signal_sent
        report["cases"].append(item)
        return item

    try:
        if drive_alias:
            drive_alias.__enter__()
            report["synthetic_drive_alias"] = drive_alias.drive
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(("host_" + nonce).encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(control_seen.wait(), 3)
        # Ordinary world-readable ACL must not defeat the package-side check.
        grant = subprocess.run(
            ["icacls.exe", str(outside), "/grant", "*S-1-5-11:(OI)(CI)M"],
            capture_output=True, timeout=10,
        )
        if grant.returncode:
            raise RuntimeError("Cannot establish synthetic outside positive control")
        report["host_control"] = {"network_reachable": True, "outside_read_write": True}
        junction = workspace / "junction"
        linked = subprocess.run(
            [str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"), "/d", "/c", "mklink", "/J",
             str(junction), str(outside)], capture_output=True, timeout=10,
        )
        if linked.returncode:
            raise RuntimeError("Cannot establish synthetic junction control")
        report["host_control"]["junction_target_readable"] = (
            (junction / "ordinary.txt").read_text() == "SYNTHETIC_ONLY"
            and junction.resolve(strict=True) == outside.resolve(strict=True)
        )
        control_process = await asyncio.create_subprocess_exec(
            str(payload), "network-control", str(port), "payload_host_" + nonce,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            control_out, control_err = await asyncio.wait_for(control_process.communicate(), 5)
            await asyncio.wait_for(payload_control_seen.wait(), 2)
            report["host_control"]["same_payload_network_reachable"] = (
                control_process.returncode == 2 and not control_err
                and control_out.decode("ascii", errors="replace").splitlines() == ["NETWORK_ALLOWED"]
            )
        except asyncio.TimeoutError:
            if control_process.returncode is None:
                control_process.kill()
                await control_process.wait()
            report["host_control"]["same_payload_network_reachable"] = False
        await run_case("cmd_smoke", ["/d", "/c", "echo", "CMD_" + nonce],
                       Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
        script = (
            "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
            f"Write-Output 'POWERSHELL_{nonce}'; "
            "Write-Output ('LANGUAGE_MODE_'+$ExecutionContext.SessionState.LanguageMode); "
            "if (-not $env:MEWCODE_SYNTHETIC_API_KEY) { Write-Output 'ENV_CLEAN' } "
            "else { Write-Output 'ENV_LEAK' }; "
            f"if ((Get-Content -LiteralPath {quote(workspace / 'input.txt')} -Raw) -eq 'SYNTHETIC_INPUT') "
            "{ Write-Output 'INPUT_OK' }; "
            f"Set-Content -LiteralPath {quote(workspace / 'output.txt')} -Value 'SYNTHETIC_OUTPUT' -NoNewline; "
            "Write-Output 'OUTPUT_OK'; "
        )
        for label, path in (("OUTSIDE_READ", outside / "ordinary.txt"), ("SECRET_READ", outside / ".env")):
            script += (
                f"try {{ $null=Get-Content -LiteralPath {quote(path)} -Raw; Write-Output '{label}_ALLOWED' }} "
                f"catch [UnauthorizedAccessException] {{ Write-Output '{label}_DENIED' }}; "
            )
        script += (
            f"try {{ Set-Content -LiteralPath {quote(outside / 'escaped.txt')} -Value 'SYNTHETIC'; "
            "Write-Output 'OUTSIDE_WRITE_ALLOWED' } "
            "catch [UnauthorizedAccessException] { Write-Output 'OUTSIDE_WRITE_DENIED' }"
        )
        previous = os.environ.get("MEWCODE_SYNTHETIC_API_KEY")
        try:
            os.environ["MEWCODE_SYNTHETIC_API_KEY"] = "SYNTHETIC_ONLY_NO_REAL_SECRET"
            boundary = await run_case("powershell_boundary", powershell_arguments(script, mta=powershell_mta), timeout_ms=powershell_timeout_ms)
            native_boundary = await run_case("exe_boundary", ["boundary", str(workspace), str(outside), str(port), nonce], payload)
            if diagnose_powershell_init:
                await run_case("powershell_init_diagnostic", ["powershell-init"], payload, timeout_ms=20000)
                await run_case("powershell_drive_diagnostic", ["powershell-drive-init"], payload, timeout_ms=20000)
            if diagnose_dotnet_threadpool:
                await run_case("dotnet_threadpool_diagnostic", ["dotnet-threadpool"], payload, timeout_ms=12000)
        finally:
            if previous is None:
                os.environ.pop("MEWCODE_SYNTHETIC_API_KEY", None)
            else:
                os.environ["MEWCODE_SYNTHETIC_API_KEY"] = previous
        # A conclusive startup is enough for independent lifecycle tests. A
        # network verdict of UNKNOWN must not suppress these observations.
        native_receipt = native_boundary.get("receipt", {})
        if (native_receipt.get("appcontainer_verified") is True
                and not native_receipt.get("error")
                and ("EXE_" + nonce) in native_receipt.get("stdout", "").splitlines()):
            for case_name, tail, timeout_ms in (
                ("root_exit_descendants", "root-exit", 10000),
                ("timeout_descendants", "timeout", 1500),
            ):
                item = await run_case(case_name, ["children", tail], payload,
                                      timeout_ms=timeout_ms)
                lines = item.get("receipt", {}).get("stdout", "").splitlines()
                pids = [int(line[10:]) for line in lines if re.fullmatch(r"CHILD_PID_\d+", line)]
                item["descendant_exit_observed"] = len(pids) == 1 and process_exited(pids[0])
            if diagnose_cancellation:
                item = await run_case("cancellation_descendants", ["children", "timeout"], payload,
                                      timeout_ms=10000, cancel_after_ms=1500)
                lines = item.get("receipt", {}).get("stdout", "").splitlines()
                pids = [int(line[10:]) for line in lines if re.fullmatch(r"CHILD_PID_\d+", line)]
                item["descendant_exit_observed"] = len(pids) == 1 and process_exited(pids[0])
        if handlers:
            await asyncio.gather(*list(handlers))
        report["observed"] = {
            "network_child_payload_received": any(
                data not in (("host_" + nonce).encode(), ("payload_host_" + nonce).encode())
                for data in received
            ),
            "outside_write_exists": (outside / "escaped.txt").exists(),
            "output_readable_by_host": (workspace / "output.txt").is_file()
                and (workspace / "output.txt").read_text() == "SYNTHETIC_OUTPUT",
            "exe_output_readable_by_host": (workspace / "native-output.txt").is_file()
                and (workspace / "native-output.txt").read_text() == "SYNTHETIC_OUTPUT",
        }
    finally:
        if drive_alias:
            try:
                drive_alias.close()
            except OSError as exc:
                report["drive_alias_cleanup_error"] = type(exc).__name__
            except RuntimeError as exc:
                report["drive_alias_cleanup_error"] = type(exc).__name__
            finally:
                report["drive_alias_cleanup_ok"] = drive_alias.cleanup_ok
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        if handlers:
            await asyncio.gather(*list(handlers), return_exceptions=True)
        revoke = subprocess.run(
            ["icacls.exe", str(outside), "/remove:g", "*S-1-5-11"], capture_output=True, timeout=10,
        )
        report["fixture_acl_cleanup_ok"] = revoke.returncode == 0
        report["assessment"] = assess_smoke(report, nonce)
        (fixture / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", required=True, type=Path)
    parser.add_argument("--execute-synthetic-probes", action="store_true", required=True)
    parser.add_argument("--regular-appcontainer", action="store_true",
                        help="explicit separate candidate, never automatic LPAC fallback")
    parser.add_argument("--runtime-registry-read", action="store_true",
                        help="explicit LPAC-only registryRead capability experiment; no network/COM capabilities")
    parser.add_argument("--runtime-instrumentation", action="store_true",
                        help="explicit lpacInstrumentation experiment; requires LPAC and --runtime-registry-read")
    parser.add_argument("--runtime-com", action="store_true",
                        help="explicit diagnostic lpacCom comparison, requires LPAC and --runtime-registry-read; never production enablement")
    parser.add_argument("--powershell-timeout-ms", type=int, default=10000,
                        help="bounded cold-start diagnostic deadline, 1000..60000 ms")
    parser.add_argument("--waitchain-helper", type=Path,
                        help="explicit absolute read-only wait-chain observer executable")
    parser.add_argument("--writable-runtime-profile", action="store_true",
                        help="grant only this fresh synthetic profile's Local/Roaming runtime directories")
    parser.add_argument("--diagnose-powershell-init", action="store_true",
                        help="fixed CLR/runspace phases and literal/native-child smoke; no user script or change to acceptance")
    parser.add_argument("--powershell-mta", action="store_true",
                        help="explicit diagnostic apartment comparison; does not change production invocation")
    parser.add_argument("--diagnose-cancellation", action="store_true",
                        help="create/signal an owned local event; verify root and descendant Job cleanup")
    parser.add_argument("--alias-workspace-drive", action="store_true",
                        help="temporary free local DOS drive alias to this synthetic workspace, exact-target cleanup")
    parser.add_argument("--managed-stack-helper", type=Path,
                        help="explicit read-only CLR stack observer, used only for this fixture's PowerShell processes")
    parser.add_argument("--diagnose-dotnet-threadpool", action="store_true",
                        help="fixed default/STA thread and ThreadPool/Task runtime comparison")
    args = parser.parse_args()
    if os.name != "nt" or not args.helper.is_absolute() or not args.helper.is_file():
        parser.error("Windows and an explicit absolute existing helper executable are required")
    if args.regular_appcontainer and args.runtime_registry_read:
        parser.error("--runtime-registry-read is only an explicit LPAC experiment")
    if args.runtime_instrumentation and (args.regular_appcontainer or not args.runtime_registry_read):
        parser.error("--runtime-instrumentation requires LPAC and --runtime-registry-read")
    if args.runtime_com and (args.regular_appcontainer or not args.runtime_registry_read):
        parser.error("--runtime-com requires LPAC and --runtime-registry-read")
    if not 1000 <= args.powershell_timeout_ms <= 60000:
        parser.error("--powershell-timeout-ms must be between 1000 and 60000")
    if args.waitchain_helper and (not args.waitchain_helper.is_absolute() or not args.waitchain_helper.is_file()):
        parser.error("--waitchain-helper must be an absolute existing executable")
    if args.managed_stack_helper and (not args.managed_stack_helper.is_absolute() or not args.managed_stack_helper.is_file()):
        parser.error("--managed-stack-helper must be an absolute existing executable")
    report = asyncio.run(experiment(args.helper, lpac=not args.regular_appcontainer,
                                    runtime_registry_read=args.runtime_registry_read,
                                    runtime_instrumentation=args.runtime_instrumentation,
                                    runtime_com=args.runtime_com,
                                    powershell_timeout_ms=args.powershell_timeout_ms,
                                    waitchain_helper=args.waitchain_helper,
                                    writable_runtime_profile=args.writable_runtime_profile,
                                    diagnose_powershell_init=args.diagnose_powershell_init,
                                    powershell_mta=args.powershell_mta,
                                    diagnose_cancellation=args.diagnose_cancellation,
                                    alias_workspace_drive=args.alias_workspace_drive,
                                    managed_stack_helper=args.managed_stack_helper,
                                    diagnose_dotnet_threadpool=args.diagnose_dotnet_threadpool))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Even a successful smoke is deliberately not a production acceptance.
    return 0 if report["assessment"]["smoke_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
