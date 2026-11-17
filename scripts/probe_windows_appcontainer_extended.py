"""Explicit synthetic LPAC escape controls, not a production authorization.

The only network destinations are host loopback listeners created by this run.
All non-system file operations stay in a fresh retained diagnostic fixture.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CAPABILITIES = ["registryRead", "lpacInstrumentation", "lpacCom"]
FILE_MARKERS = ("INPUT_OK", "JUNCTION_WRITE_DENIED", "AUTH_GRANTS_READ_DENIED",
                "AUTH_GRANTS_WRITE_DENIED", "AUTH_RULES_READ_DENIED", "AUTH_RULES_WRITE_DENIED", "FILES_DONE")
REQUIRED_CHECKS = ("file_boundary", "junction_write_absent", "authority_files_unchanged",
                   "tcp4", "tcp6", "udp4", "udp6", "fixture_acl_cleanup")


def valid_execution(case: dict, markers: tuple[str, ...], *, exit_code: int = 0) -> bool:
    receipt = case.get("receipt", {})
    lines = str(receipt.get("stdout", "")).splitlines()
    return (
        case.get("helper_exit_code") == exit_code and receipt.get("exit_code") == exit_code
        and not receipt.get("error") and receipt.get("timed_out") is False
        and receipt.get("cancelled") is False and receipt.get("capabilities") == CAPABILITIES
        and receipt.get("lpac_verified") is True and receipt.get("lpac_access_mask") == 2
        and all(receipt.get(key) is True for key in (
            "job_cleanup_ok", "acl_cleanup_ok", "profile_cleanup_ok", "appcontainer_verified"))
        and receipt.get("stdout_truncated") is False and receipt.get("stderr_truncated") is False
        and all(lines.count(marker) == 1 for marker in markers)
    )


def extended_smoke_passed(report: dict) -> bool:
    checks = report.get("checks", {})
    return not report.get("error") and all(checks.get(key) is True for key in REQUIRED_CHECKS)


async def experiment(helper: Path) -> dict:
    from mewcode.sandbox.appcontainer_policy import build_probe_request
    from scripts.native_probe_resources import ProbeCancelEvent

    fixture = Path(tempfile.mkdtemp(prefix="mewcode-lpac-probe-", dir=ROOT / ".tmp"))
    (fixture / ".mewcode-synthetic-fixture").write_bytes(b"mewcode-synthetic-only-v1\n")
    workspace, outside = fixture / "workspace", fixture / "outside"
    for directory in (workspace / "temp", workspace / ".mewcode/rules", outside,
                      fixture / "profile/AppData/Local", fixture / "profile/AppData/Roaming"):
        directory.mkdir(parents=True, exist_ok=True)
    (workspace / "input.txt").write_text("SYNTHETIC_INPUT", encoding="utf-8")
    authority_paths = [workspace / ".mewcode/permissions.grants.json", workspace / ".mewcode/rules/host.rules"]
    for path in authority_paths:
        path.write_text("SYNTHETIC_HOST_AUTHORITY", encoding="utf-8")
    (outside / "ordinary.txt").write_text("SYNTHETIC_ONLY", encoding="utf-8")
    payload = workspace / "escape-payload.exe"
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    built = subprocess.run([str(compiler), "/nologo", "/platform:x64", "/out:" + str(payload),
                            str(ROOT / "native/windows/MewCode.EscapePayload.cs")],
                           capture_output=True, timeout=30)
    if built.returncode:
        raise RuntimeError("Escape payload compilation failed: " + built.stdout.decode(errors="replace")[:4000])
    nonce = uuid.uuid4().hex
    report = {"schema": "mewcode-extended-native-experiment-v1", "fixture": str(fixture),
              "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
              "payload_sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
              "cases": [], "controls": {}, "checks": {}, "production_authorized": False}
    listeners = []
    handlers: set[asyncio.Task] = set()
    received: dict[str, list[bytes]] = {mode: [] for mode in ("tcp4", "tcp6", "udp4", "udp6")}

    async def run_case(name, arguments):
        request = build_probe_request(
            fixture_root=fixture, executable=payload, arguments=arguments, cwd=workspace,
            read_paths=[workspace, workspace / "temp", workspace / "input.txt"],
            write_paths=[workspace, workspace / "temp"], timeout_ms=6000,
            lpac=True, runtime_registry_read=True, runtime_instrumentation=True, runtime_com=True,
        )
        with ProbeCancelEvent() as event:
            request["cancel_event_name"] = event.name
            request_path = fixture / (name + ".request.json")
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            process = await asyncio.create_subprocess_exec(
                str(helper), str(request_path), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            capture = asyncio.create_task(process.communicate())
            try:
                out, err = await asyncio.wait_for(asyncio.shield(capture), 20)
                try:
                    receipt = json.loads(out.decode("utf-8-sig"))
                except (UnicodeError, ValueError):
                    receipt = {"error": "invalid_helper_receipt"}
                case = {"name": name, "helper_exit_code": process.returncode, "receipt": receipt,
                        "helper_stderr": err.decode(errors="replace")[:4000]}
            finally:
                if process.returncode is None:
                    event.signal()
                    try:
                        await asyncio.wait_for(asyncio.shield(capture), 8)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        # Closing the helper's Job handle kills its descendants,
                        # but missing finally evidence is never cleanup success.
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                if not capture.done():
                    capture.cancel()
                await asyncio.gather(capture, return_exceptions=True)
        report["cases"].append(case)
        return case

    async def accept(mode, reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            received[mode].append(await asyncio.wait_for(reader.read(256), 3))
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    class Datagram(asyncio.DatagramProtocol):
        def __init__(self, mode):
            self.mode = mode

        def datagram_received(self, data, address):
            received[self.mode].append(data[:256])

    try:
        granted = subprocess.run(["icacls.exe", str(outside), "/grant", "*S-1-5-11:(OI)(CI)M"],
                                 capture_output=True, timeout=10)
        if granted.returncode:
            raise RuntimeError("Synthetic outside ACL positive control failed")
        linked = subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"),
                                 "/d", "/c", "mklink", "/J", str(workspace / "junction"), str(outside)],
                                capture_output=True, timeout=10)
        if linked.returncode:
            raise RuntimeError("Synthetic junction creation failed")
        report["controls"]["junction_readable"] = (workspace / "junction/ordinary.txt").read_text() == "SYNTHETIC_ONLY"
        files = await run_case("files", ["files", str(workspace), str(outside), nonce])
        report["checks"]["file_boundary"] = valid_execution(files, FILE_MARKERS)
        # Marker contract is supplied by the fixed payload, never a model.
        report["checks"]["junction_write_absent"] = not (outside / "junction-escaped.txt").exists()
        report["checks"]["authority_files_unchanged"] = all(
            path.read_text(encoding="utf-8") == "SYNTHETIC_HOST_AUTHORITY" for path in authority_paths)

        for mode in received:
            family = socket.AF_INET6 if mode.endswith("6") else socket.AF_INET
            address = "::1" if mode.endswith("6") else "127.0.0.1"
            try:
                if mode.startswith("tcp"):
                    listener = await asyncio.start_server(
                        lambda r, w, m=mode: accept(m, r, w), address, 0, family=family)
                    port = listener.sockets[0].getsockname()[1]
                else:
                    listener, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                        lambda m=mode: Datagram(m), local_addr=(address, 0), family=family)
                    port = listener.get_extra_info("sockname")[1]
                listeners.append(listener)
            except OSError as exc:
                report["controls"][mode] = {"available": False, "error": type(exc).__name__}
                report["checks"][mode] = False
                continue
            host_nonce, child_nonce = "host_" + mode + "_" + nonce, "child_" + mode + "_" + nonce
            control = await asyncio.create_subprocess_exec(
                str(payload), "network", mode, str(port), host_nonce, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                out, err = await asyncio.wait_for(control.communicate(), 5)
            finally:
                if control.returncode is None:
                    control.kill()
                    await control.wait()
            for _ in range(30):
                if host_nonce.encode() in received[mode]:
                    break
                await asyncio.sleep(0.01)
            host_ok = (control.returncode == 2 and not err and host_nonce.encode() in received[mode]
                       and out.decode(errors="replace").splitlines() == ["NETWORK_" + mode.upper() + "_ALLOWED"])
            report["controls"][mode] = {"available": True, "same_payload_reachable": host_ok}
            case = await run_case(mode, ["network", mode, str(port), child_nonce])
            # Every listener stays open until all children exit, catching any
            # delayed child delivery rather than just its own immediate case.
            report["checks"][mode] = host_ok and valid_execution(case, ("NETWORK_" + mode.upper() + "_DENIED",))
        await asyncio.sleep(0.1)
        for mode in received:
            child_nonce = ("child_" + mode + "_" + nonce).encode()
            if child_nonce in received[mode]:
                report["checks"][mode] = False
            report["controls"].setdefault(mode, {})["child_payload_received"] = child_nonce in received[mode]
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)[:500]
    finally:
        for listener in listeners:
            listener.close()
            if hasattr(listener, "wait_closed"):
                await listener.wait_closed()
        if handlers:
            await asyncio.gather(*list(handlers), return_exceptions=True)
        revoked = subprocess.run(["icacls.exe", str(outside), "/remove:g", "*S-1-5-11"],
                                 capture_output=True, timeout=10)
        report["checks"]["fixture_acl_cleanup"] = revoked.returncode == 0
        report["production_authorized"] = False
        (fixture / "extended-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", required=True, type=Path)
    parser.add_argument("--execute-synthetic-probes", required=True, action="store_true")
    args = parser.parse_args()
    if os.name != "nt" or not args.helper.is_absolute() or not args.helper.is_file():
        parser.error("Windows and an explicit absolute helper executable are required")
    report = asyncio.run(experiment(args.helper))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if extended_smoke_passed(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
