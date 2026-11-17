"""Host-only metadata broker protocol negatives; synthetic files only.

Never shortens the production lifetime or changes the helper. Each launch
uses normal UAC, which the user must confirm. Lifetime mode takes 20 minutes.
"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mewcode.sandbox import metadata_broker as mb
from mewcode.sandbox.native_gate import installed_bundle_identity, machine_guid
from scripts.manage_native_runtime import verify_bundle
from scripts.native_acceptance_executor import bounded_directory


def send(client, payload):
    count = W.DWORD()
    assert client.k.WriteFile(client.pipe, payload, len(payload), C.byref(count), None)
    assert count.value == len(payload)


def response(client, body):
    raw = body.encode()
    send(client, struct.pack("<I", len(raw)) + raw)
    length, = struct.unpack("<I", client._read(4))
    assert 0 < length <= mb.MAX_FRAME
    return json.loads(client._read(length))


def ping(client):
    # A valid authenticated request with no active transaction must refuse
    # the action but still return the bound reply. No scope/file is opened.
    try:
        client.request("end")
    except mb.BrokerError as error:
        assert "No metadata transaction scope is active" in str(error)
    else:
        raise AssertionError("End outside a transaction unexpectedly succeeded")


def exited(client, *, timeout=8):
    assert client.k.WaitForSingleObject(client.process, int(timeout * 1000)) == 0, "broker did not exit"
    get = client.k.GetExitCodeProcess
    get.argtypes, get.restype = [W.HANDLE, C.POINTER(W.DWORD)], W.BOOL
    code = W.DWORD()
    assert get(client.process, C.byref(code))
    return code.value


def main(args):
    bundle = args.bundle.resolve(strict=True)
    assert verify_bundle(bundle)["ok"], "bundle verification failed"
    artifact = mb.installed_artifact(bundle / "MewCode.AppContainerRuntime.exe")
    assert artifact is not None
    if args.mode == "orphan-child":
        client = mb.Broker(*artifact)
        ping(client)
        # A venv python.exe may be a redirector: Popen.pid can identify that
        # launcher, not this process which actually owns the pipe handle.
        print(json.dumps({"pipe": int(client.pipe), "pid": client.k.GetProcessId(client.process),
                          "owner_pid": os.getpid()}), flush=True)
        assert sys.stdin.readline().strip() == "exit-synthetic-parent"
        os._exit(0)  # Deliberate synthetic parent loss, not normal pipe-close cleanup.
    fixture = bounded_directory(args.fixture, must_exist=False)
    fixture.mkdir()
    report = {"schema": "mewcode-metadata-protocol-probe-v1", "mode": args.mode,
        "bundle_identity": installed_bundle_identity(bundle), "machine_guid": machine_guid(),
        "helper_sha256": artifact[1], "complete": False, "cases": []}
    def record(name, **facts):
        report["cases"].append({"case": name, "ok": True, **facts})
        print(name + ": passed", flush=True)
    try:
        if args.mode == "lifetime":
            client = mb.Broker(*artifact)
            started = time.monotonic()
            try:
                ping(client)
                while client.alive and time.monotonic() - started < 1230:
                    time.sleep(1)
                    elapsed = time.monotonic() - started
                    if int(elapsed) % 60 == 0 and client.alive:
                        ping(client)  # Activity must not reset the session deadline.
                        print("lifetime observation: " + str(int(elapsed)) + "s", flush=True)
                elapsed = time.monotonic() - started
                code = exited(client)
                assert 1190 <= elapsed <= 1230 and code == 124
                record("session_deadline_enforced", elapsed_seconds=round(elapsed, 3), exit_code=code)
            finally:
                client.close()
        else:
            client = mb.Broker(*artifact)
            try:
                ping(client)
                answer = response(client, json.dumps({"nonce": "0" * 64, "seq": 2, "op": "end"}))
                assert answer["ok"] is False and "nonce mismatch" in answer["error"]
                ping(client)
                record("wrong_nonce_denied")
                answer = response(client, '{"nonce":' + json.dumps(client.nonce) + ',"seq":3,"seq":3,"op":"end"}')
                assert answer["ok"] is False and "Duplicate" in answer["error"]
                ping(client)
                record("duplicate_fields_denied")
                send(client, struct.pack("<I", mb.MAX_FRAME + 1))
                code = exited(client)
                # InvalidDataException is an IOException in this runtime;
                # an invalid frame closes the pipe through the normal EOF
                # cleanup branch, not necessarily the outer exception branch.
                assert code in (0, 1)
                record("oversized_frame_denied", exit_code=code)
            finally:
                client.close()
            client = mb.Broker(*artifact)
            try:
                ping(client)
                started = time.monotonic()
                send(client, struct.pack("<I", 100) + b"{")
                assert exited(client, timeout=20) == 124
                assert 14 <= time.monotonic() - started <= 20
                record("incomplete_frame_timeout")
            finally:
                client.close()
            # A wrong *live synthetic* parent must fail server-PID binding.
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            saved_os = mb.os
            try:
                mb.os = SimpleNamespace(name="nt", getpid=lambda: child.pid)
                client = mb.Broker(*artifact)
                try:
                    client.request("end")
                finally:
                    client.close()
                raise AssertionError("Wrong server PID was accepted")
            except mb.BrokerError as error:
                assert "elevation was not granted" not in str(error)
                assert "connection failed" in str(error) or "transport failed" in str(error) or "not running" in str(error)
                record("wrong_server_pid_denied")
            finally:
                mb.os = saved_os
                child.terminate(); child.wait(5)
            parent = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--bundle", str(bundle),
                "--mode", "orphan-child"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            kernel = mb._kernel()
            kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
            kernel.DuplicateHandle.argtypes = [W.HANDLE, W.HANDLE, W.HANDLE, C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD]
            kernel.DuplicateHandle.restype = W.BOOL
            pin_parent = pin_helper = retained_pipe = None
            try:
                info = json.loads(parent.stdout.readline())
                pin_parent = kernel.OpenProcess(0x40 | 0x100000, False, info["owner_pid"])
                pin_helper = kernel.OpenProcess(0x100000, False, info["pid"])
                if not pin_parent or not pin_helper:
                    raise C.WinError(C.get_last_error())
                retained_pipe = W.HANDLE()
                if not kernel.DuplicateHandle(pin_parent, info["pipe"], W.HANDLE(-1), C.byref(retained_pipe), 0, False, 2):
                    raise C.WinError(C.get_last_error())
                # A helper that died before parent loss is not evidence that
                # parent monitoring works, even if its pinned handle remains.
                assert kernel.WaitForSingleObject(pin_parent, 0) == 258, "parent exited before trigger"
                assert kernel.WaitForSingleObject(pin_helper, 0) == 258, "broker exited before parent-loss trigger"
                parent.stdin.write("exit-synthetic-parent\n"); parent.stdin.flush()
                assert parent.wait(10) == 0
                assert kernel.WaitForSingleObject(pin_parent, 8000) == 0
                # Keeping the server pipe alive distinguishes parent monitoring
                # from the unrelated ordinary EOF shutdown path.
                assert kernel.WaitForSingleObject(pin_helper, 8000) == 0
                record("parent_exit_enforced_with_pipe_retained")
            finally:
                for handle in (retained_pipe, pin_helper, pin_parent):
                    if handle:
                        kernel.CloseHandle(handle)
                if parent.poll() is None:
                    parent.terminate(); parent.wait(5)
        report["complete"] = True
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        (fixture / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--mode", choices=("quick", "lifetime", "orphan-child"), required=True)
    raise SystemExit(main(parser.parse_args()))
