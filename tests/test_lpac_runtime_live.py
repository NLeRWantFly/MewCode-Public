"""Explicit local-only acceptance of the dormant production LPAC transport.

All inputs/output are fresh synthetic files; no business run, external host,
provider, permanent machine setting or production activation is touched.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
from types import SimpleNamespace

import pytest

from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.sandbox import lpac_runtime as lpac

pytestmark = pytest.mark.skipif(os.name != "nt" or not os.environ.get("MEWCODE_TEST_LPAC_RUNTIME_EXE"),
    reason="explicit compiled LPAC helper and local acceptance opt-in required")


_NUL_ACCESS_PROBE = r"""
using System;
using System.Runtime.InteropServices;

public static class MewCodeNulAccessProbe
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFileW(string path, uint access, uint share, IntPtr security,
        uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ReadFile(IntPtr handle, byte[] buffer, uint count, out uint read, IntPtr overlapped);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool WriteFile(IntPtr handle, byte[] buffer, uint count, out uint written, IntPtr overlapped);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    public static string Run(string label, string path, string mode, bool directory)
    {
        uint access = mode == "read" ? 0x80000000u : mode == "write" ? 0x40000000u : 0xC0000000u;
        uint flags = directory ? 0x02000000u : 0x80u;
        IntPtr handle = CreateFileW(path, access, 3, IntPtr.Zero, 3, flags, IntPtr.Zero);
        if (handle == IntPtr.Zero || handle == new IntPtr(-1))
            return label + ":open=failed:win32=" + Marshal.GetLastWin32Error().ToString();

        string result = label + ":open=ok";
        byte[] buffer = new byte[] { 0x58 };
        uint transferred;
        if (mode == "read" || mode == "readwrite")
        {
            if (ReadFile(handle, buffer, 1, out transferred, IntPtr.Zero))
                result += ":read=ok:bytes=" + transferred.ToString();
            else
                result += ":read=failed:win32=" + Marshal.GetLastWin32Error().ToString();
        }
        if (mode == "write" || mode == "readwrite")
        {
            if (WriteFile(handle, buffer, 1, out transferred, IntPtr.Zero))
                result += ":write=ok:bytes=" + transferred.ToString();
            else
                result += ":write=failed:win32=" + Marshal.GetLastWin32Error().ToString();
        }
        CloseHandle(handle);
        return result;
    }

    public static int Main(string[] args)
    {
        if (args.Length != 1) return 2;
        Console.WriteLine(Run("nul-read", "NUL", "read", false));
        Console.WriteLine(Run("nul-write", "NUL", "write", false));
        Console.WriteLine(Run("nul-readwrite", "NUL", "readwrite", false));
        Console.WriteLine(Run("host-profile-read", args[0], "read", true));
        Console.WriteLine(Run("host-profile-write", args[0], "write", true));
        return 0;
    }
}
"""


_PIN_POLICY_PROBE = r"""
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

public static class MewCodePinPolicyProbe
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFileW(string path, uint access, uint share, IntPtr security,
        uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    private static object NewRequest(Type requestType, Type grantType)
    {
        object request = Activator.CreateInstance(requestType, true);
        Type grantsType = typeof(Dictionary<,>).MakeGenericType(typeof(string), grantType);
        requestType.GetField("Grants").SetValue(request, Activator.CreateInstance(grantsType));
        requestType.GetField("Executable").SetValue(request,
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "System32\\cmd.exe"));
        return request;
    }

    private static void AddGrant(object request, Type requestType, Type grantType, string path)
    {
        object grant = Activator.CreateInstance(grantType, true);
        grantType.GetField("Path").SetValue(grant, path);
        object grants = requestType.GetField("Grants").GetValue(request);
        grants.GetType().GetMethod("Add").Invoke(grants, new object[] { path, grant });
    }

    private static bool Check(MethodInfo method, string path, object request)
    {
        return (bool)method.Invoke(null, new object[] { path, request });
    }

    public static int Main(string[] args)
    {
        Assembly assembly = Assembly.LoadFrom(args[0]);
        Type requestType = assembly.GetType("MewCode.Native.Windows.ValidatedRequest", true);
        Type grantType = assembly.GetType("MewCode.Native.Windows.PathGrant", true);
        Type probeType = assembly.GetType("MewCode.Native.Windows.MewCodeAppContainerProbe", true);
        MethodInfo method = probeType.GetMethod("IsUnmodifiableHostProfileAncestor",
            BindingFlags.NonPublic | BindingFlags.Static);
        if (method == null) return 2;

        string profile = Environment.GetEnvironmentVariable("USERPROFILE");
        IntPtr attributes = CreateFileW(profile, 0x80, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
        if (attributes != IntPtr.Zero && attributes != new IntPtr(-1))
        {
            CloseHandle(attributes);
            return 7;
        }
        if (Marshal.GetLastWin32Error() != 5) return 8;
        object request = NewRequest(requestType, grantType);
        AddGrant(request, requestType, grantType, Path.Combine(profile, "synthetic-workspace", "input.txt"));
        if (!Check(method, profile, request)) return 3;
        if (Check(method, Path.Combine(profile, "synthetic-workspace"), request)) return 4;
        AddGrant(request, requestType, grantType, profile);
        if (Check(method, profile, request)) return 5;

        string writableProfile = Path.GetFullPath(args[1]);
        Environment.SetEnvironmentVariable("USERPROFILE", writableProfile);
        object writableRequest = NewRequest(requestType, grantType);
        AddGrant(writableRequest, requestType, grantType,
            Path.Combine(writableProfile, "synthetic-workspace", "input.txt"));
        if (Check(method, writableProfile, writableRequest)) return 6;

        Console.WriteLine("PROFILE_BOUNDARY_EXEMPTION_OK");
        return 0;
    }
}
"""


def _powershell_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _compile_nul_access_probe(case):
    root, runtime, _, _ = case
    source = runtime / "nul-access-probe.cs"
    compiled = runtime / "nul-access-probe.exe"
    staged = root / compiled.name
    source.write_text(_NUL_ACCESS_PROBE, encoding="utf-8")
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    result = subprocess.run([str(compiler), "/nologo", "/platform:x64", "/out:" + str(compiled),
        str(source)], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
    shutil.copy2(compiled, staged)
    return staged


def _nul_diagnostic_command(probe_executable, profile):
    return "& " + _powershell_quote(probe_executable) + " " + _powershell_quote(profile) + "; exit $LASTEXITCODE"


def _host_nul_diagnostic():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create_file.restype = ctypes.c_void_p
    read_file = kernel32.ReadFile
    read_file.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    read_file.restype = ctypes.c_int
    write_file = kernel32.WriteFile
    write_file.argtypes = read_file.argtypes
    write_file.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    invalid = ctypes.c_void_p(-1).value
    report = {}
    for mode, access in (("read", 0x80000000), ("write", 0x40000000), ("readwrite", 0xC0000000)):
        ctypes.set_last_error(0)
        handle = create_file("NUL", access, 3, None, 3, 0x80, None)
        opened = handle not in (None, invalid)
        entry = {"open": opened, "open_win32": 0 if opened else ctypes.get_last_error()}
        if opened:
            buffer = ctypes.create_string_buffer(b"X", 1)
            transferred = ctypes.c_uint32(0)
            if mode in {"read", "readwrite"}:
                ctypes.set_last_error(0)
                succeeded = bool(read_file(handle, buffer, 1, ctypes.byref(transferred), None))
                entry["read"] = {"ok": succeeded, "bytes": transferred.value,
                    "win32": 0 if succeeded else ctypes.get_last_error()}
            if mode in {"write", "readwrite"}:
                ctypes.set_last_error(0)
                succeeded = bool(write_file(handle, buffer, 1, ctypes.byref(transferred), None))
                entry["write"] = {"ok": succeeded, "bytes": transferred.value,
                    "win32": 0 if succeeded else ctypes.get_last_error()}
            close_handle(handle)
        report[mode] = entry
    return report


def test_profile_pin_exception_is_narrow_and_unmodifiable(native_case):
    _, runtime, helper, _ = native_case
    source = runtime / "pin-policy-probe.cs"
    executable = runtime / "pin-policy-probe.exe"
    source.write_text(_PIN_POLICY_PROBE, encoding="utf-8")
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    compiled = subprocess.run([str(compiler), "/nologo", "/platform:x64", "/out:" + str(executable),
        str(source)], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert compiled.returncode == 0, compiled.stdout.decode(errors="replace") + compiled.stderr.decode(errors="replace")
    result = subprocess.run([str(executable), str(helper), str(runtime)], capture_output=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
    assert b"PROFILE_BOUNDARY_EXEMPTION_OK" in result.stdout


@pytest.fixture
def native_case(tmp_path):
    root = tmp_path / "合成 workspace"
    root.mkdir()
    runtime = tmp_path / "private-host"
    for path in ("temp", "profile/AppData/Local", "profile/AppData/Roaming"):
        (runtime / path).mkdir(parents=True)
    (root / "input.txt").write_text("SYNTHETIC_INPUT", encoding="utf-8")
    (root / ".mewcode").mkdir()
    (root / ".mewcode/permissions.yaml").write_text("SYNTHETIC_AUTHORITY", encoding="utf-8")
    (tmp_path / ".env").write_text("SYNTHETIC_SECRET", encoding="utf-8")
    helper = lpac.checked_path(Path(os.environ["MEWCODE_TEST_LPAC_RUNTIME_EXE"]), file=True)
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(root)), RuleEngine(), PermissionMode.AUTO)
    scope = lpac.NativeScope.from_checker(checker)
    return root, runtime, helper, scope


async def run_case(case, command, timeout=10):
    root, runtime, helper, scope = case
    cancel = lpac.HostCancelEvent()
    try:
        request = lpac.build_runtime_request(command, scope, runtime, timeout=timeout, cancel_name=cancel.name)
        result = await lpac.invoke_helper(helper, request, cancel)
        (runtime / (request["request_id"] + ".receipt.json")).write_text(json.dumps(result.__dict__, indent=2), encoding="utf-8")
        return result
    finally:
        cancel.close()


def stage_test_bundle(case, monkeypatch):
    """Exercise installed-path discovery in private test data, never the app's data."""
    from mewcode.sandbox import windows
    root, _, helper, _ = case
    userdata = root.parent / "isolated-installed-data"
    bundle = userdata / "native/windows-lpac-v1"
    bundle.mkdir(parents=True)
    shutil.copy2(helper, bundle / helper.name)
    shutil.copy2(helper.parent / "manifest.json", bundle / "manifest.json")
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(userdata))
    # Test-process-only injection. Source gates and installed app remain off.
    monkeypatch.setattr(windows, "native_runtime_ready", lambda: True)
    return userdata


@pytest.mark.asyncio
async def test_live_bash_uses_staged_host_bundle_and_persists_receipt(native_case, monkeypatch):
    from mewcode.tools.bash import Bash, Params as BashParams
    from mewcode.tools.command_sandbox import native_auto_command
    root, _, _, scope = native_case
    userdata = stage_test_bundle(native_case, monkeypatch)
    owner = SimpleNamespace(_native_cleanup_error="")
    with native_auto_command(scope.checker, owner=owner):
        result = await Bash().run(BashParams(command="Write-Output HOST_ADAPTER_OK", timeout=10), work_dir=str(root))
    assert not result.is_error and "mewcode-windows-lpac" in result.output, result
    assert "HOST_ADAPTER_OK" in result.output and not owner._native_cleanup_error
    receipts = list((userdata / "native/runs").glob("*/receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["status"] == "exited" and receipt["cleanup_confirmed"] is True


@pytest.mark.asyncio
async def test_live_powershell_reads_writes_and_rejects_outside_and_authority(native_case):
    root, runtime, _, _ = native_case
    quote = lambda path: "'" + str(path).replace("'", "''") + "'"
    command = "$ErrorActionPreference='Stop'; " + (
        "if ((Get-Content -LiteralPath 'input.txt') -ne 'SYNTHETIC_INPUT') { exit 3 }; "
        "Set-Content -LiteralPath 'output.txt' -Value 'SYNTHETIC_OUTPUT'; "
        f"try {{ Get-Content -LiteralPath {quote(root.parent / '.env')}; exit 4 }} catch [UnauthorizedAccessException] {{}}; "
        f"try {{ Get-Content -LiteralPath {quote(root / '.mewcode/permissions.yaml')}; exit 5 }} catch [UnauthorizedAccessException] {{}}; "
        f"try {{ Set-Content -LiteralPath {quote(root.parent / 'escaped.txt')} -Value 'SYNTHETIC'; exit 6 }} catch [UnauthorizedAccessException] {{}}; "
        "if ($env:OPENAI_API_KEY) { exit 7 }; "
        "Write-Output 'LPAC_RUNTIME_OK'; exit 0")
    result = await run_case(native_case, command)
    assert result.confirms_stdout_line("LPAC_RUNTIME_OK"), result
    assert result.cleanup_confirmed
    assert (root / "output.txt").read_text().strip() == "SYNTHETIC_OUTPUT"
    assert not (root.parent / "escaped.txt").exists()


@pytest.mark.asyncio
async def test_live_exe_dependencies_and_timeout(native_case):
    result = await run_case(native_case, "cmd.exe /c echo LPAC_EXE_OK; exit $LASTEXITCODE")
    assert result.confirms_stdout_line("LPAC_EXE_OK"), result
    result = await run_case(native_case, "Start-Sleep -Seconds 20", timeout=1)
    assert result.status == "timed_out" and result.cleanup_confirmed, result


@pytest.mark.asyncio
async def test_live_cancellation_persists_cleanup_before_task_finishes(native_case):
    root, runtime, helper, scope = native_case
    cancel = lpac.HostCancelEvent()
    receipts = []
    task = None
    try:
        request = lpac.build_runtime_request("Write-Output STARTED; Start-Sleep -Seconds 20", scope, runtime,
            timeout=25, cancel_name=cancel.name)
        task = asyncio.create_task(lpac.invoke_helper(helper, request, cancel, on_receipt=receipts.append))
        await asyncio.sleep(2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(receipts) == 1 and receipts[0].cleanup_confirmed, receipts
        assert receipts[0].status == "cancelled"
        (runtime / "cancel-receipt.json").write_text(json.dumps(receipts[0].__dict__), encoding="utf-8")
    finally:
        if task is not None and not task.done():
            cancel.signal()
            await asyncio.gather(task, return_exceptions=True)
        cancel.close()


@pytest.mark.asyncio
async def test_live_parallel_scopes_do_not_share_writes(native_case):
    first = await run_case(native_case, "Write-Output PARALLEL_ONE")
    assert first.confirms_stdout_line("PARALLEL_ONE"), first
    # Same workspace requests get distinct ephemeral SIDs, not shared grants.
    results = await asyncio.gather(run_case(native_case, "Write-Output PARALLEL_A"),
                                   run_case(native_case, "Write-Output PARALLEL_B"))
    assert all(not r.is_error and r.cleanup_confirmed for r in results), results


@pytest.mark.asyncio
async def test_live_powershell_unicode_output(native_case):
    result = await run_case(native_case,
        "Write-Output '中文回执'")
    assert result.confirms_stdout_line("中文回执"), result


@pytest.mark.asyncio
async def test_live_profile_ancestor_is_ungranted_and_nul_access_is_measured(native_case):
    root, runtime, _, scope = native_case
    host_profile = Path(os.path.abspath(os.environ["USERPROFILE"]))
    assert root.is_relative_to(host_profile), root
    cancel = lpac.HostCancelEvent()
    try:
        request = lpac.build_runtime_request("Write-Output NUL_DIAGNOSTIC_OK", scope, runtime,
            timeout=15, cancel_name=cancel.name)
        assert str(host_profile) not in request["read_paths"]
        assert str(host_profile) not in request["write_paths"]
        assert request["environment"]["USERPROFILE"] != str(host_profile)
    finally:
        cancel.close()

    host_report = _host_nul_diagnostic()
    assert all(item["open"] for item in host_report.values()), host_report
    assert host_report["read"]["read"] == {"ok": True, "bytes": 0, "win32": 0}, host_report
    assert host_report["write"]["write"] == {"ok": True, "bytes": 1, "win32": 0}, host_report
    assert host_report["readwrite"]["read"]["ok"] and host_report["readwrite"]["write"]["ok"], host_report

    probe_executable = _compile_nul_access_probe(native_case)
    result = await run_case(native_case, _nul_diagnostic_command(probe_executable, host_profile), timeout=15)
    (runtime / "nul-diagnostics.json").write_text(json.dumps({"host": host_report,
        "lpac_receipt": result.__dict__}, indent=2), encoding="utf-8")
    assert not result.is_error and result.cleanup_confirmed, result
    lines = set(result.stdout.splitlines())
    assert "nul-read:open=failed:win32=5" in lines, result
    assert "nul-write:open=failed:win32=5" in lines, result
    assert "nul-readwrite:open=failed:win32=5" in lines, result
    assert "host-profile-read:open=failed:win32=5" in lines, result
    assert "host-profile-write:open=failed:win32=5" in lines, result


@pytest.mark.asyncio
async def test_live_copied_python_venv_without_granting_installed_runtime(native_case, monkeypatch):
    from mewcode.experts.node_expansion import ExpansionPathSandbox
    root, runtime, helper, _ = native_case
    source = Path(sys.base_prefix)
    copied = root / "python-runtime"
    copied.mkdir()
    # Copy only this installed interpreter's runtime into our synthetic data.
    # No ACL change on the user's installation, and no package downloads.
    for path in source.iterdir():
        if path.is_file() and path.suffix.lower() in {".exe", ".dll"}:
            shutil.copy2(path, copied / path.name)
    for name in ("Lib", "DLLs"):
        shutil.copytree(source / name, copied / name,
            ignore=shutil.ignore_patterns("__pycache__", "site-packages", "test", "tests", "ensurepip"))
    created = subprocess.run([str(copied / "python.exe"), "-m", "venv", "--without-pip", str(root / ".venv")],
        capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
    assert created.returncode == 0, created.stderr.decode(errors="replace")
    (root / "test_local.py").write_text("import unittest\nclass Local(unittest.TestCase):\n def test_ok(self): self.assertEqual(2+2,4)\n", encoding="utf-8")
    (root / "test_exit.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    (root / "output").mkdir()
    checker = PermissionChecker(DangerousCommandDetector(),
        ExpansionPathSandbox(PathSandbox(str(root)), (str(root / "output"),)), RuleEngine(), PermissionMode.AUTO)
    case = root, runtime, helper, lpac.NativeScope.from_checker(checker)
    result = await run_case(case, ".\\.venv\\Scripts\\python.exe -m unittest -q test_local; exit $LASTEXITCODE", timeout=30)
    assert not result.is_error and result.cleanup_confirmed, result
    assert "OK" in result.stderr or "OK" in result.stdout
    from mewcode.tools.command_sandbox import native_auto_command
    from mewcode.tools.run_verification import RunVerification, RunVerificationParams
    stage_test_bundle(case, monkeypatch)
    with native_auto_command(checker):
        verified = await RunVerification().run(RunVerificationParams(
            command=r".\.venv\Scripts\python.exe -m unittest -q test_local"), work_dir=str(root))
    assert not verified.is_error, verified
    payload = json.loads(verified.output)
    assert payload["exit_code"] == 0 and payload["isolation"] == lpac.BACKEND
    with native_auto_command(checker):
        failed = await RunVerification().run(RunVerificationParams(
            command=r".\.venv\Scripts\python.exe -m unittest -q test_exit"), work_dir=str(root))
    assert failed.is_error and json.loads(failed.output)["exit_code"] == 7, failed


@pytest.mark.asyncio
async def test_live_copied_git_status(native_case):
    root, runtime, helper, _ = native_case
    installed = Path(shutil.which("git") or "missing").resolve()
    if not installed.is_file() or not (installed.parent.parent / "mingw64/bin/git.exe").is_file():
        pytest.skip("Git for Windows runtime is not installed")
    git = root / "git-runtime"
    (git / "cmd").mkdir(parents=True)
    (git / "mingw64/bin").mkdir(parents=True)
    shutil.copy2(installed, git / "cmd/git.exe")
    installed_bin = installed.parent.parent / "mingw64/bin"
    for path in installed_bin.iterdir():
        if path.suffix.lower() == ".dll" or path.name == "git.exe":
            shutil.copy2(path, git / "mingw64/bin" / path.name)
    initialized = subprocess.run([str(installed), "init", "--quiet", str(root)], capture_output=True,
        timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    assert initialized.returncode == 0
    # Prove the copied runtime itself works before attributing its failure to
    # LPAC. No real HOME, user Git config, repository or network is used.
    system = os.environ["SystemRoot"]
    positive_environment = {"SystemRoot": system, "WINDIR": system,
        "PATH": str(Path(system) / "System32"), "TEMP": str(runtime / "temp"),
        "TMP": str(runtime / "temp"), "HOME": str(runtime / "profile"),
        "USERPROFILE": str(runtime / "profile"), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0"}
    commands = {
        "version": [str(git / "cmd/git.exe"), "--version"],
        "status_fsmonitor_disabled": [str(git / "cmd/git.exe"), "-C", str(root),
            "-c", "core.fsmonitor=false", "status", "--porcelain"],
    }
    host_reports = {}
    for name, argv in commands.items():
        positive = subprocess.run(argv, env=positive_environment, capture_output=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW)
        host_reports[name] = {"exit_code": positive.returncode,
            "stdout": positive.stdout.decode(errors="replace"),
            "stderr": positive.stderr.decode(errors="replace")}
        assert positive.returncode == 0, host_reports[name]

    commands_in_powershell = {
        "version": ".\\git-runtime\\cmd\\git.exe --version; exit $LASTEXITCODE",
        "status_fsmonitor_disabled": ".\\git-runtime\\cmd\\git.exe -c core.fsmonitor=false status --porcelain; exit $LASTEXITCODE",
    }
    lpac_reports = {}
    for name, command in commands_in_powershell.items():
        result = await run_case(native_case, command, timeout=15)
        lpac_reports[name] = result.__dict__
    (runtime / "git-nul-diagnostics.json").write_text(json.dumps({"host": host_reports,
        "lpac": lpac_reports}, indent=2), encoding="utf-8")
    assert all(report["cleanup_confirmed"] is True for report in lpac_reports.values()), lpac_reports
    assert lpac_reports["version"]["exit_code"] == 0, lpac_reports["version"]
    assert lpac_reports["status_fsmonitor_disabled"]["exit_code"] == 0, lpac_reports["status_fsmonitor_disabled"]
