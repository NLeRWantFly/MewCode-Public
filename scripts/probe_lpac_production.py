"""Live WP4 probe: run the real LPAC chain against synthetic fixtures.

This executes the *installed* native bundle end to end — helper, bundled Git,
UTF-8 capture, .git write authorization, boundary matrix. Default mode uses
the enabled production path. Explicit --diagnostic uses a host-only adapter
limited to declared project .tmp fixtures, without writing gate records.

Usage:
    python scripts/probe_lpac_production.py --receipts <out.json> \
        --fixture-root <C: dir> [--e-fixture-root <E: dir>]

Each fixture gets a CJK/space workspace with a Git repository and a venv.
Every case records its receipt; any failed assertion marks the case failed
and the process exits nonzero. Nothing outside the fixture roots, the probe
data root, and one loopback listener is touched.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mewcode.permissions import (  # noqa: E402
    DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine,
)
from mewcode.permissions.task_authorization import TaskAuthorization  # noqa: E402
from mewcode.sandbox import lpac_runtime as lpac  # noqa: E402
from mewcode.sandbox.native_gate import NativeGateState, native_gate_status  # noqa: E402
from mewcode.sandbox.native_evidence import SCHEMA  # noqa: E402

PROJECT_VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
CJK_TEXT = "中文输出：稳定验证"
CJK_FILE = "未跟踪 文件.txt"
CJK_TRACKED = "说明 文档.txt"
CJK_COMMIT = "提交：新增中文文件"
WORKSPACE_NAME = "中文 workspace 空格"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class ProbeFailure(RuntimeError):
    pass


def report_summary(report: dict) -> dict:
    """Selected-case success is distinct from the fixed release matrix."""
    from mewcode.sandbox.native_evidence import REQUIRED_CASES, evidence_passes
    records = report["cases"]
    executed = [r for r in records if r["status"] != "not_run"]
    missing = [key for key in REQUIRED_CASES if not evidence_passes(
        report, key, report["bundle_identity"], report["machine_guid"])]
    return {
        "executed_cases": len(executed),
        "failed": [f"{r['fixture']}:{r['case']}" for r in executed if not r["ok"]],
        "not_run": [f"{r['fixture']}:{r['case']}" for r in records if r["status"] == "not_run"],
        "missing_evidence": missing,
        "selected_cases_passed": bool(executed) and all(r["ok"] for r in executed),
        "full_matrix_passed": not missing and bool(executed) and all(r["ok"] for r in executed),
    }


def run_host(arguments: list[str], *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    result = subprocess.run(arguments, cwd=str(cwd), capture_output=True, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result


def prepare_fixture(fixture_root: Path, bundled_git: Path, host_python: Path,
                    *, fresh: bool = False, isolate_python: bool = False) -> dict:
    workspace = fixture_root / WORKSPACE_NAME
    repo = workspace / "repository"
    venv = workspace / ".venv"
    if fresh and fixture_root.exists():
        # Never bulk-delete; retire any previous fixture by rename instead.
        import time
        retired = fixture_root.with_name(fixture_root.name + f".retired-{int(time.time())}")
        counter = 2
        while retired.exists():
            retired = fixture_root.with_name(f"{fixture_root.name}.retired-{int(time.time())}-{counter}")
            counter += 1
        os.rename(fixture_root, retired)
    if repo.is_dir() and (venv / "Scripts" / "python.exe").is_file():
        cfg = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
        home = next(line.split("=", 1)[1].strip() for line in cfg.splitlines()
                    if line.lower().startswith("home"))
        return {"workspace": workspace, "repo": repo, "venv": venv,
                "venv_python": venv / "Scripts" / "python.exe", "base_python": Path(home)}
    repo.mkdir(parents=True, exist_ok=True)
    (repo / CJK_TRACKED).write_text("第一版内容\n", encoding="utf-8")
    host_config = fixture_root / "host-empty.gitconfig"
    host_config.write_text("", encoding="ascii")
    templates = fixture_root / "host-empty-template"
    templates.mkdir()
    git_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_GLOBAL": str(host_config), "HOME": str(fixture_root),
               "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@localhost",
               "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@localhost"}
    init = subprocess.run([str(bundled_git), "-C", str(repo), "-c", "init.defaultBranch=main",
                           "init", "--quiet", "--template=" + str(templates)], env=git_env, capture_output=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if init.returncode:
        raise ProbeFailure(f"host git init failed: {init.stderr!r}")
    for args in (["add", "--", CJK_TRACKED], ["commit", "--quiet", "-m", "fixture baseline"]):
        step = subprocess.run([str(bundled_git), "-C", str(repo), *args], env=git_env,
                              capture_output=True, timeout=60,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if step.returncode:
            raise ProbeFailure(f"host git {args[0]} failed: {step.stderr!r}")
    # Tool runtime inside the workspace (granted opaque read-only by the
    # boundary) plus its base install outside (exact directory read grant).
    if isolate_python:
        located = run_host([str(host_python), "-I", "-c", "import json,sys; print(json.dumps(sys.base_prefix))"], cwd=workspace)
        if located.returncode:
            raise ProbeFailure("Cannot resolve the fixture Python base install")
        base_source = Path(json.loads(located.stdout)).resolve(strict=True)
        # Fresh copy: the diagnostic helper must never modify ACEs on the
        # user's shared Python installation, even temporarily.
        copied_base = fixture_root / "python-runtime"
        shutil.copytree(base_source, copied_base, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        host_python = copied_base / "python.exe"
    created = run_host([str(host_python), "-m", "venv", "--without-pip", str(venv)], cwd=workspace, timeout=600)
    if created.returncode:
        raise ProbeFailure(f"venv creation failed: {created.stderr!r}")
    tests_dir = workspace / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_sample.py").write_text(
        "import unittest\n\n\nclass SampleTest(unittest.TestCase):\n"
        "    def test_arithmetic(self):\n        self.assertEqual(2 + 2, 4)\n\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n", encoding="utf-8")
    cfg = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    home = next(line.split("=", 1)[1].strip() for line in cfg.splitlines()
                if line.lower().startswith("home"))
    base = Path(home)
    if not base.is_dir():
        raise ProbeFailure(f"venv base interpreter missing: {base}")
    return {"workspace": workspace, "repo": repo, "venv": venv,
            "venv_python": venv / "Scripts" / "python.exe", "base_python": base}


def build_checker(workspace: Path) -> PermissionChecker:
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(workspace)),
                                RuleEngine(), PermissionMode.AUTO)
    scope = TaskAuthorization.from_user_scope({
        "read_directories": [str(workspace)],
        "write_directories": [str(workspace)],
        "allowed_behaviors": ["isolated_command", "git_write"],
        "ttl_seconds": 3600,
    }, task_id="probe-task")
    checker.set_task_authorization(scope, task_id="probe-task")
    return checker


def _process_alive(pid: int) -> bool:
    if os.name != "nt":
        raise ProbeFailure("Process verification is Windows-only")
    if pid <= 0:
        raise ProbeFailure("Invalid child process id")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    # HANDLE is pointer-sized: the default ctypes c_int silently truncates it
    # on 64-bit Windows. An open handle also does not mean a process is alive.
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: the process no longer exists
            return False
        raise ProbeFailure(f"Cannot verify child process termination (Windows error {error})")
    try:
        wait = kernel.WaitForSingleObject(handle, 0)
        if wait not in (0, 258):  # WAIT_OBJECT_0 / WAIT_TIMEOUT
            raise ProbeFailure("Child process termination wait failed")
        return wait == 258
    finally:
        kernel.CloseHandle(handle)


def require(record: dict, condition: bool, message: str) -> None:
    """Add assertions without erasing transport/cleanup failures."""
    if not condition:
        record["failures"].append(message)
    record["ok"] = not record["failures"]


def verify_child_reaped(record: dict, pid_file: Path) -> None:
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
        alive = _process_alive(pid)
    except (ValueError, OSError, ProbeFailure) as exc:
        require(record, False, f"Child cleanup could not be verified: {exc}")
        return
    record["child_reaped"] = not alive
    require(record, not alive, "spawned child survived job cleanup")


async def run_case(checker: PermissionChecker, workspace: Path, extra_read: tuple[Path, ...],
                   name: str, command: str, *, expect_exit: int | None = 0,
                   expect_stdout: tuple[str, ...] = (), reject_stdout: tuple[str, ...] = (),
                   timeout: int = 120, executor=None) -> dict:
    scope = lpac.NativeScope.from_checker(checker)
    receipt = await (executor or lpac.execute_lpac)(command, scope=scope, work_dir=str(workspace),
                                      timeout=timeout, extra_read=extra_read)
    record = {"case": name, "command": command, "status": receipt.status,
              "exit_code": receipt.exit_code, "stdout": receipt.stdout,
              "stderr": receipt.stderr, "backend": receipt.backend,
              "cleanup_confirmed": receipt.cleanup_confirmed, "detail": receipt.detail,
              "output_truncated": receipt.output_truncated}
    record.update(transaction_id=receipt.transaction_id, publication_state=receipt.publication_state)
    failures: list[str] = []
    if receipt.status != "exited":
        failures.append(f"status={receipt.status} detail={receipt.detail}")
    if expect_exit is not None and receipt.exit_code != expect_exit:
        failures.append(f"exit_code={receipt.exit_code} expected={expect_exit}")
    for marker in expect_stdout:
        if marker not in receipt.stdout:
            failures.append(f"stdout missing {marker!r}")
    for marker in reject_stdout:
        if marker in receipt.stdout or marker in receipt.stderr:
            failures.append(f"output unexpectedly contains {marker!r}")
    if receipt.cleanup_confirmed is not True:
        failures.append("cleanup unconfirmed")
    if receipt.output_truncated:
        failures.append("probe output truncated")
    record["failures"] = failures
    record["ok"] = not failures
    return record


async def run_cancel_case(checker, workspace: Path, extra_read, *, executor=None,
                          start_timeout: float = 30) -> dict:
    """Cancel only after the child PID was durably observed, not after a sleep."""
    pid_file = workspace / "cancel-child.pid"
    if pid_file.exists():
        raise ProbeFailure("Cancellation fixture must not contain an old child PID")
    command = (
        "$p = Start-Process -PassThru -NoNewWindow -FilePath 'powershell.exe' "
        "-ArgumentList '-NoProfile -NonInteractive -Command Start-Sleep -Seconds 90'; "
        "Set-Content -LiteralPath 'cancel-child.pid' -Value $p.Id -Encoding ascii; "
        "Start-Sleep -Seconds 90")
    captured = []
    runner = executor or lpac.execute_lpac
    task = asyncio.create_task(runner(command, scope=lpac.NativeScope.from_checker(checker),
                                     work_dir=str(workspace), timeout=120,
                                     extra_read=extra_read, on_receipt=captured.append))
    record = {"case": "process_tree_cancelled", "status": "unknown", "exit_code": None,
              "backend": lpac.BACKEND, "cleanup_confirmed": False, "output_truncated": False,
              "failures": [], "ok": False}
    deadline = asyncio.get_running_loop().time() + start_timeout
    try:
        started = False
        while not task.done() and asyncio.get_running_loop().time() < deadline:
            try:
                if executor is not None and hasattr(executor, "observed_path"):
                    pid_file = executor.observed_path(workspace, "cancel-child.pid")
                started = _process_alive(int(pid_file.read_text(encoding="ascii").strip()))
            except (OSError, ValueError, ProbeFailure):
                started = False
            if started:
                break
            await asyncio.sleep(0.05)
        record["child_started"] = started
        require(record, started, "live child startup was not observed before cancellation")
    finally:
        if not task.done():
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), 35)
        except asyncio.CancelledError:
            pass
        except (TimeoutError, RuntimeError, OSError, ValueError) as exc:
            require(record, False, f"cancellation cleanup failed: {exc}")
    if captured:
        receipt = captured[-1]
        record.update(status=receipt.status, exit_code=receipt.exit_code,
                      cleanup_confirmed=receipt.cleanup_confirmed,
                      output_truncated=receipt.output_truncated)
    require(record, record["status"] == "cancelled", "cancelled host receipt missing")
    require(record, record["cleanup_confirmed"] is True, "cancellation cleanup unconfirmed")
    verify_child_reaped(record, pid_file)
    return record


async def run_fixture(label: str, prepared: dict, bundled_git: Path, receipts_dir: Path,
                      *, executor=None, on_case=None, selected: frozenset[str] = frozenset()) -> list[dict]:
    workspace: Path = prepared["workspace"]
    repo: Path = prepared["repo"]
    checker = build_checker(workspace)
    records: list[dict] = []
    outside = workspace.parent / "ungranted-target"
    outside.mkdir(exist_ok=True)
    (outside / "sentinel.txt").write_text("SYNTHETIC_UNGRANTED_SENTINEL", encoding="ascii")

    async def case(name: str, command: str, **kwargs) -> dict:
        if selected and name not in selected:
            record = {"case": name, "status": "not_run", "exit_code": None, "stdout": "", "stderr": "",
                      "detail": "not selected", "backend": lpac.BACKEND, "cleanup_confirmed": None,
                      "output_truncated": False, "failures": ["not selected"], "ok": False}
        else:
            # Production must derive Python dependencies from the frozen
            # virtualenv configuration. Supplying them only from a fixture
            # hid the desktop adapter's missing dependency mapping.
            record = await run_case(checker, workspace, (), name, command, executor=executor, **kwargs)
        record["fixture"] = label
        records.append(record)
        if on_case:
            on_case(record)
        # A broker can renew midway through a long matrix. A cancelled UAC
        # must end this invocation, not trigger another prompt on every
        # remaining case. Persist the failed receipt before stopping.
        if "Metadata broker elevation was not granted" in record.get("detail", ""):
            raise ProbeFailure("Elevation was not granted; remaining cases were not run: " + record["detail"])
        if name == "powershell_cjk" and record["status"] not in {"exited", "not_run"}:
            raise ProbeFailure("Native startup prerequisite failed; remaining cases were not run: " + record["detail"])
        return record

    import time
    (repo / CJK_FILE).write_text(f"新文件内容 {int(time.time())}\n", encoding="utf-8")

    await case("powershell_cjk", f"Write-Output {_ps_quote(CJK_TEXT)}",
               expect_stdout=(CJK_TEXT,))
    await case("workspace_file_roundtrip",
               "Set-Content -LiteralPath '.\\created-by-child.txt' -Value 'first' -Encoding utf8; "
               "Get-Content -LiteralPath '.\\created-by-child.txt'; "
               "Set-Content -LiteralPath '.\\created-by-child.txt' -Value 'second' -Encoding utf8; "
               "Get-Content -LiteralPath '.\\created-by-child.txt'",
               expect_stdout=("first", "second"))
    created_file = workspace / "created-by-child.txt"
    require(records[-1], created_file.is_file() and created_file.read_text(encoding="utf-8-sig").strip() == "second",
            "host did not observe the second workspace file version")
    await case("git_version", "git --version", expect_stdout=("git version 2.55.0.windows.3",))
    git_exe = str(bundled_git)
    await case("wrapper_debug",
               f"$cap = Join-Path $env:TEMP 'wrapper-debug.raw'; "
               f"if (Test-Path $cap) {{ Remove-Item $cap -Force }}; "
               f"$p = Start-Process -PassThru -NoNewWindow -Wait -FilePath {_ps_quote(git_exe)} "
               "-ArgumentList '--version' -RedirectStandardOutput $cap; "
               "Write-Output ('exit=' + $p.ExitCode + ' size=' + (Get-Item $cap).Length); "
               "Get-Content -Raw -Encoding UTF8 $cap",
               expect_stdout=("exit=0", "git version"))
    await case("git_status_cjk", "Set-Location repository; git status --porcelain",
               expect_stdout=(CJK_FILE,))
    head = repo / ".git" / "refs" / "heads" / "main"
    initial_head = head.read_bytes()
    await case("git_add_commit",
               f"Set-Location repository; git add -A; "
               f"git commit -m {_ps_quote(CJK_COMMIT)}; git log -1 --format=%s; git status --porcelain",
               expect_stdout=(CJK_COMMIT,))
    require(records[-1], head.read_bytes() != initial_head, "first commit did not advance the host-observed ref")
    (repo / CJK_TRACKED).write_text("第二版内容：已修改\n", encoding="utf-8")
    await case("git_diff_cjk",
               "Set-Location repository; git diff --stat",
               expect_stdout=(CJK_TRACKED,))
    first_head = head.read_bytes()
    await case("git_second_commit",
               "Set-Location repository; git add -A; "
               "git commit -m '第二版'; git status --porcelain",
               reject_stdout=(CJK_FILE,))
    require(records[-1], head.read_bytes() != first_head, "second commit did not advance the host-observed ref")
    config_before = (repo / ".git" / "config").read_bytes()
    await case("git_config_denied",
               "Set-Location repository; Write-Output 'CONFIG_ATTEMPT'; git config user.name injected; exit $LASTEXITCODE",
               expect_exit=None, expect_stdout=("CONFIG_ATTEMPT",))
    config_record = records[-1]
    require(config_record, any(r["case"] == "git_version" and r["ok"] for r in records),
            "Git execution prerequisite did not pass")
    require(config_record, config_record["exit_code"] not in (0, None),
            "git config write was not refused")
    require(config_record, (repo / ".git" / "config").read_bytes() == config_before,
            "repository config changed on disk")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("SYNTHETIC_PROTECTED_HOOK\n", encoding="ascii")
    await case("git_hook_write_denied",
               "Set-Location repository; Write-Output 'HOOK_ATTEMPT'; "
               "Set-Content -Path '.git/hooks/pre-commit' -Value 'injected' -ErrorAction Stop",
               expect_exit=None, expect_stdout=("HOOK_ATTEMPT",))
    hook_record = records[-1]
    require(hook_record, hook_record["exit_code"] not in (0, None),
            "hook write was not refused")
    hook_content = (repo / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8", errors="replace") \
        if (repo / ".git" / "hooks" / "pre-commit").exists() else ""
    hook_record["hook_intact"] = "injected" not in hook_content
    require(hook_record, hook_record["hook_intact"], "hook file content changed on disk")
    await case("python_cjk",
               f"& {_ps_quote('.venv\\Scripts\\python.exe')} -c \"print('{CJK_TEXT}')\"",
               expect_stdout=(CJK_TEXT,))
    await case("python_unittest",
               f"& {_ps_quote('.venv\\Scripts\\python.exe')} -m unittest discover -s tests",
               expect_stdout=(), expect_exit=0)
    unittest_record = records[-1]
    if "OK" not in unittest_record["stderr"]:
        unittest_record["ok"] = False
        unittest_record["failures"] = [*unittest_record["failures"], "unittest OK line missing"]
    bundle_manifest = Path(os.environ["MEWCODE_USER_DATA_DIR"]) / "native" / "windows-lpac-v1" / "manifest.json"
    await case("sensitive_read_denied",
               f"try {{ Get-Content -LiteralPath {_ps_quote(str(bundle_manifest))} -ErrorAction Stop | Out-Null; "
               "Write-Output 'SENSITIVE_READ_UNEXPECTED' } catch { Write-Output 'SENSITIVE_READ_DENIED' }",
               expect_stdout=("SENSITIVE_READ_DENIED",), reject_stdout=("SENSITIVE_READ_UNEXPECTED",))
    temp_escape = outside / "escape-write.txt"
    await case("escape_write_denied",
               f"try {{ New-Item -Path {_ps_quote(str(temp_escape))} -ItemType File -ErrorAction Stop | Out-Null; "
               "Write-Output 'ESCAPE_WRITE_UNEXPECTED' } catch { Write-Output 'ESCAPE_WRITE_DENIED' }; "
               "try { Set-Content -LiteralPath '..\\cleanup\\receipt.json' -Value 'forged' -ErrorAction Stop; "
               "Write-Output 'CLEANUP_FORGED' } catch { Write-Output 'CLEANUP_WRITE_DENIED' }",
               expect_stdout=("ESCAPE_WRITE_DENIED", "CLEANUP_WRITE_DENIED"),
               reject_stdout=("ESCAPE_WRITE_UNEXPECTED", "CLEANUP_FORGED"))
    escape_record = records[-1]
    if temp_escape.exists():
        # Never delete from the probe; report and leave the file in place.
        escape_record["ok"] = False
        escape_record["failures"] = [*escape_record["failures"], "escape file exists on host"]
    await case("secret_env_scrubbed",
               "$names = Get-ChildItem env: | Select-Object -ExpandProperty Name; "
               "if ($names -match 'KEY|TOKEN|SECRET|PROXY') { Write-Output 'ENV_LEAK' } "
               "else { Write-Output 'ENV_SCRUBBED' }",
               expect_stdout=("ENV_SCRUBBED",), reject_stdout=("ENV_LEAK",))
    # Loopback denial: a live host listener must stay unreachable.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(15)
    port = listener.getsockname()[1]
    accepted: list[bool] = []

    def watch() -> None:
        try:
            conn, _ = listener.accept()
            accepted.append(True)
            conn.close()
        except (OSError, socket.timeout):
            pass

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    await case("network_loopback_denied",
               f"try {{ $response = Invoke-WebRequest -Uri 'http://127.0.0.1:{port}/' -TimeoutSec 5 "
               "-UseBasicParsing -ErrorAction Stop; Write-Output 'LOOPBACK_CONNECTED' } "
               "catch { Write-Output 'LOOPBACK_DENIED' }",
               expect_stdout=("LOOPBACK_DENIED",), reject_stdout=("LOOPBACK_CONNECTED",), timeout=60)
    watcher.join(timeout=20)
    listener.close()
    network_record = records[-1]
    if accepted:
        network_record["ok"] = False
        network_record["failures"] = [*network_record["failures"], "host listener accepted a connection"]
    pid_file = workspace / "child.pid"
    await case("process_tree_reaped",
               "$p = Start-Process -PassThru -NoNewWindow -FilePath 'powershell.exe' "
               "-ArgumentList '-NoProfile -NonInteractive -Command Start-Sleep -Seconds 90'; "
               "Set-Content -Path (Join-Path $env:TEMP 'child.pid') -Value $p.Id -Encoding ascii; "
               "if ($p.HasExited) { throw 'child exited before root cleanup' }; Write-Output 'SPAWNED_ALIVE'",
               expect_stdout=("SPAWNED_ALIVE",), timeout=60)
    tree_record = records[-1]
    tree_record["child_started"] = "SPAWNED_ALIVE" in tree_record["stdout"]
    if executor is not None and hasattr(executor, "observed_path") and tree_record["status"] != "not_run":
        pid_file = executor.observed_path(workspace, "child.pid")
    elif tree_record.get("transaction_id"):
        from mewcode.sandbox.transaction_registry import TransactionRegistry
        transaction = TransactionRegistry().get(tree_record["transaction_id"])
        if transaction["workspace"] != str(workspace):
            raise ProbeFailure("Process receipt is bound to another workspace")
        pid_file = Path(transaction["transaction"]) / "runtime/temp/child.pid"
    verify_child_reaped(tree_record, pid_file)
    if not selected or "process_tree_cancelled" in selected:
        cancelled = await run_cancel_case(checker, workspace, (), executor=executor)
        cancelled["fixture"] = label
        records.append(cancelled)
        if on_case:
            on_case(cancelled)
    else:
        # Keep every matrix member visible even for targeted invocations.
        await case("process_tree_cancelled", "")
    # Last: preserve any created link as evidence, never recursively clean it.
    # The target is an ungranted synthetic sibling, not system/user data.
    await case("junction_escape_denied",
               "$created = $false; "
               f"try {{ New-Item -ItemType Junction -Path '.\\probe-junction' -Target {_ps_quote(str(outside))} "
               "-ErrorAction Stop | Out-Null; $created = $true } catch {}; "
               "if ($created) { $traversable = $false; "
               "try { Get-Content '.\\probe-junction\\sentinel.txt' -ErrorAction Stop | Out-Null; "
               "$traversable = $true } catch {}; "
               "if ($traversable) { Write-Output 'JUNCTION_TRAVERSABLE' } "
               "else { Write-Output 'JUNCTION_CREATED_UNUSABLE' } } "
               "else { Write-Output 'JUNCTION_DENIED' }",
               expect_exit=0, reject_stdout=("JUNCTION_TRAVERSABLE",))
    junction_record = records[-1]
    require(junction_record, any(marker in junction_record["stdout"] for marker in
                                 ("JUNCTION_DENIED", "JUNCTION_CREATED_UNUSABLE")),
            "junction outcome marker missing")
    link = workspace / "probe-junction"
    if link.exists() or link.is_symlink():
        info = link.lstat()
        junction_record["host_reparse_observed"] = bool(getattr(info, "st_file_attributes", 0) & 0x400
                                                       or stat.S_ISLNK(info.st_mode))
        # A failed New-Item may leave an ordinary empty directory. It is not
        # a junction and must not be reported as an escape.
        require(junction_record, not junction_record["host_reparse_observed"]
                or "JUNCTION_CREATED_UNUSABLE" in junction_record["stdout"],
                "host saw an unverified reparse point")
    receipts_dir.mkdir(parents=True, exist_ok=True)
    return records


async def async_main(args: argparse.Namespace) -> int:
    executor = None
    if args.diagnostic:
        from scripts.native_acceptance_executor import AcceptanceExecutor, bounded_directory, bounded_fixture
        bounded_directory(args.receipts.parent, must_exist=False)
        if os.path.lexists(args.receipts):
            raise ProbeFailure("Diagnostic receipt path must be new; prior evidence is never overwritten")
        fixture = bounded_directory(args.fixture_root, must_exist=False)
        args.fixture_root = fixture
        if os.path.lexists(fixture):
            raise ProbeFailure("Diagnostic fixtures must be fresh; choose a new .tmp directory")
        declared = [fixture]
        if args.e_fixture_root is not None:
            args.e_fixture_root = bounded_fixture(args.e_fixture_root)
            if os.path.lexists(args.e_fixture_root):
                raise ProbeFailure("External diagnostic fixtures must be fresh")
            declared.append(args.e_fixture_root)
        executor = AcceptanceExecutor(Path(os.environ["MEWCODE_USER_DATA_DIR"]), tuple(declared))
        receipt_path = args.receipts.absolute()
        if any(receipt_path.is_relative_to(p) for p in declared) or receipt_path.is_relative_to(executor.data_root):
            raise ProbeFailure("Diagnostic reports must stay outside the child fixture and native data root")
    gate = native_gate_status()
    if not args.diagnostic and gate.state is not NativeGateState.ENABLED:
        print(json.dumps({"error": f"probe requires an enabled gate, state={gate.state.value}",
                          "reason": gate.reason}, ensure_ascii=False))
        return 2
    helper = lpac.find_runtime_helper()
    from mewcode.sandbox.native_gate import machine_guid, installed_bundle_identity
    identity, machine = installed_bundle_identity(helper.parent), machine_guid()
    bundled_git = helper.parent / "git" / "git.exe"
    if not bundled_git.is_file():
        print(json.dumps({"error": "installed bundle has no git/git.exe"}))
        return 2
    fixtures: list[tuple[str, Path]] = [("c-volume", args.fixture_root)]
    if args.e_fixture_root is not None:
        fixtures.append(("e-volume", args.e_fixture_root))
    all_records: list[dict] = []
    complete = False
    args.receipts.parent.mkdir(parents=True, exist_ok=True)

    def flush() -> None:
        payload = {"schema": SCHEMA, "bundle_identity": identity, "machine_guid": machine,
                   "complete": complete,
                   "execution_path": "host-diagnostic" if args.diagnostic else "production",
                   "bundle_helper": str(helper), "cases": all_records,
                   "ok": all(record["ok"] for record in all_records)}
        args.receipts.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    for label, fixture_root in fixtures:
        prepared = prepare_fixture(fixture_root, bundled_git, args.python, fresh=args.fresh,
                                   isolate_python=args.diagnostic)
        def on_case(record):
            all_records.append(record)
            flush()
        await run_fixture(label, prepared, bundled_git, args.receipts.parent,
                          executor=executor, on_case=on_case, selected=frozenset(args.case or ()))
        flush()
    complete = True
    flush()
    summary = report_summary({"schema": SCHEMA, "complete": complete,
                              "bundle_identity": identity, "machine_guid": machine,
                              "cases": all_records})
    print(json.dumps({"receipts": str(args.receipts), **summary}, ensure_ascii=False))
    # Targeted invocations retain their useful exit status, but the JSON
    # cannot call a C:-only or partial selection a complete release matrix.
    passed = summary["selected_cases_passed"] if args.case else summary["full_matrix_passed"]
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receipts", required=True, type=Path)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--e-fixture-root", type=Path, default=None)
    parser.add_argument("--python", type=Path, default=PROJECT_VENV_PYTHON,
                        help="host interpreter used to create the fixture venv "
                             "(must resolve to a non-reparse base install)")
    parser.add_argument("--fresh", action="store_true", help="retire and rebuild fixtures")
    parser.add_argument("--diagnostic", action="store_true",
                        help="host-only acceptance in fresh project .tmp fixtures; never enables AUTO")
    from mewcode.sandbox.native_evidence import REQUIRED_CASES
    parser.add_argument("--case", action="append", choices=sorted(
        {case for cases in REQUIRED_CASES.values() for case in cases} | {"wrapper_debug"}),
                        help="run a targeted case; all other cases stay explicitly not_run")
    args = parser.parse_args()
    if os.name != "nt":
        print("Windows-only probe")
        return 2
    if not os.environ.get("MEWCODE_USER_DATA_DIR", "").strip():
        print("MEWCODE_USER_DATA_DIR must point at the probe data root")
        return 2
    try:
        return asyncio.run(async_main(args))
    except (ProbeFailure, OSError, ValueError, lpac.NativeBoundaryUnavailable) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
