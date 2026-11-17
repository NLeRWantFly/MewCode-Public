"""Detached dependency snapshots and candidate-local verification.

No source .git/dependency junctions are installed. This prevents accidental
shared-environment edits, not arbitrary malicious test subprocess side effects;
the command permission layer and original-file conflict checks still apply.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib

from mewcode.experts.audit_workspace import manifest, _atomic_json
from mewcode.experts.audit_protocol import content_hash
from mewcode.tools.bash import _terminate_process_tree, _shell_command

MAX_DEP_FILES = 100_000
MAX_DEP_BYTES = 2 * 1024 * 1024 * 1024
PYTHON_MARKERS = ("requirements.txt", "requirements-dev.txt", "uv.lock", "poetry.lock")
NODE_MARKERS = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "npm-shrinkwrap.json")


def environment_info(source, candidate):
    source, candidate = Path(source), Path(candidate)
    python = None
    sites = []
    for name in (".venv", "venv"):
        root = source / name
        executable = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if executable.is_file():
            python = str(executable.absolute())
            sites = [str(p) for p in ([root / "Lib/site-packages"] if os.name == "nt" else root.glob("lib/python*/site-packages")) if p.is_dir()]
            break
    return {"candidate_dir": str(candidate), "source_dir": str(source), "python": python or sys.executable,
            "python_origin": "project_venv" if python else "host_runtime",
            "python_dependencies": sites, "node_dependencies": str(source / "node_modules") if (source / "node_modules").is_dir() else None,
            "git": "not_copied; use the manifest diff returned by VerifyCandidate",
            "dependency_mode": "detached_copy_on_first_verification; no shared writable links",
            "limitations": "No install/network setup is automatic; dependency changes or unavailable runners must be reported, not counted as test success."}


def _dependency_compatible(source, candidate, runner):
    for name in NODE_MARKERS if runner == "npm_test" else PYTHON_MARKERS:
        first, second = source / name, candidate / name
        if first.exists() != second.exists() or (first.exists() and first.read_bytes() != second.read_bytes()):
            raise ValueError(f"依赖声明已改变（{name}）；VerifyCandidate 不能复用原依赖。请改用候选专用匹配环境的显式验证命令并提交日志，不得修改原环境。")
    if runner != "npm_test":
        def requirements(root):
            path = root / "pyproject.toml"
            data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            project, config = data.get("project", {}), data.get("tool", {})
            poetry = config.get("poetry", {})
            return ({key: project.get(key) for key in ("dependencies", "optional-dependencies", "requires-python", "dynamic")},
                    {key: poetry.get(key) for key in ("dependencies", "dev-dependencies", "group")},
                    data.get("build-system", {}), data.get("dependency-groups", {}),
                    config.get("uv", {}), config.get("pdm", {}))
        if requirements(source) != requirements(candidate):
            raise ValueError("pyproject.toml 依赖已改变；需要候选专用的匹配环境，不能复用原依赖验证。")
        return
    first, second = source / "package.json", candidate / "package.json"
    if first.exists() or second.exists():
        a = json.loads(first.read_text(encoding="utf-8")) if first.exists() else {}
        b = json.loads(second.read_text(encoding="utf-8")) if second.exists() else {}
        if any(a.get(key) != b.get(key) for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")):
            raise ValueError("package.json 依赖已改变；需要候选专用的匹配依赖，不能复用原环境验证。")


def _local_path(root, path):
    """Reject even excluded-directory junctions before touching runtime files."""
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root) or ".." in path.relative_to(root).parts:
        raise ValueError("候选验证路径离开候选目录")
    current = root
    for part in ("", *path.relative_to(root).parts):
        current = current / part if part else current
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"候选验证目录不允许链接/重解析点：{current.name}")
        if stat.S_ISREG(info.st_mode) and getattr(info, "st_nlink", 1) > 1:
            raise ValueError(f"候选验证目录不允许共享文件硬链接：{current.name}")
    return path


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("候选依赖复制已停止；未完成快照已保留，不会复用")


def _validate_snapshot(root, path, cancel):
    _local_path(root, path)
    if not path.is_dir():
        raise ValueError("候选依赖快照已缺失，不能计为验证成功")
    count = 0
    for current, directories, files in os.walk(path, followlinks=False):
        _check_cancel(cancel)
        for name in directories + files:
            _local_path(root, Path(current) / name)
            count += 1
            if count > MAX_DEP_FILES * 2:
                raise ValueError("候选依赖快照超过验证扫描上限")


def copy_dependencies(source, destination, *, cancel=None, budget=None):
    """Copy bytes, resolving only links inside this dependency tree; never link."""
    source, destination = Path(source).resolve(strict=True), Path(destination)
    if destination.exists():
        raise ValueError("依赖目标已存在但未完成验证，禁止覆盖；请保留现场并重新准备候选环境")
    budget = budget if budget is not None else {"files": 0, "bytes": 0}

    def copy_entry(current, target, ancestors):
        _check_cancel(cancel)
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(source):
            raise ValueError(f"依赖链接离开依赖目录，未复制：{current.name}")
        if resolved.is_dir():
            if resolved in ancestors:
                raise ValueError("依赖目录出现链接循环")
            target.mkdir()
            for child in sorted(resolved.iterdir()):
                if child.name in {"__pycache__", ".cache", ".git"}:
                    continue
                copy_entry(child, target / child.name, ancestors | {resolved})
            return
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("依赖只允许普通文件")
        budget["files"] += 1
        budget["bytes"] += info.st_size
        if budget["files"] > MAX_DEP_FILES or budget["bytes"] > MAX_DEP_BYTES:
            raise ValueError("依赖快照超过 100000 文件/2 GiB；请提供精简候选验证环境")
        with resolved.open("rb") as src, target.open("xb") as dst:
            while chunk := src.read(1024 * 1024):
                _check_cancel(cancel)
                dst.write(chunk)
        shutil.copystat(resolved, target)

    destination.parent.mkdir(parents=True, exist_ok=True)
    copy_entry(source, destination, set())
    return dict(budget)


def prepare_environment(source, candidate, runner, *, cancel=None):
    _check_cancel(cancel)
    source, candidate = Path(source).resolve(strict=True), Path(candidate).resolve(strict=True)
    _dependency_compatible(source, candidate, runner)
    info = environment_info(source, candidate)
    _check_cancel(cancel)
    runtime = _local_path(candidate, candidate / ".mewcode" / "verification")
    runtime.mkdir(parents=True, exist_ok=True)
    family = "node" if runner == "npm_test" else "python"
    marker = _local_path(candidate, runtime / f"{family}-dependencies.json")
    signature = content_hash({"source": str(source), "python": info["python"], "sites": info["python_dependencies"],
                              "node": info["node_dependencies"], "family": family})
    if marker.exists():
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if saved.get("signature") != signature:
            raise ValueError("候选验证环境来源已变化；不能静默复用旧依赖")
        copied_sites = [Path(path) for path in saved.get("copied_python_dependencies", [])]
        if family == "python" and len(copied_sites) != len(info["python_dependencies"]):
            raise ValueError("候选依赖快照记录不完整")
        for path in copied_sites:
            if not path.is_relative_to(runtime):
                raise ValueError("候选依赖快照离开验证目录")
            _validate_snapshot(candidate, path, cancel)
        if family == "node" and info["node_dependencies"]:
            _validate_snapshot(candidate, candidate / "node_modules", cancel)
        return {**info, "copied_python_dependencies": [str(path) for path in copied_sites], "runtime_dir": str(runtime)}
    # Unique staging retains interrupted evidence without blocking a fresh copy.
    staging = Path(tempfile.mkdtemp(prefix=f"{family}-snapshot-", dir=runtime))
    budget = {"files": 0, "bytes": 0}
    copied_sites = []
    for index, directory in enumerate(info["python_dependencies"] if family == "python" else []):
        target = staging / f"site-{index}"
        copy_dependencies(directory, target, cancel=cancel, budget=budget)
        copied_sites.append(str(target))
    if family == "node":
        target = _local_path(candidate, candidate / "node_modules")
        if target.exists():
            raise ValueError("候选 node_modules 已存在且不属于已完成快照；保留现场，禁止静默覆盖")
        if info["node_dependencies"]:
            copy_dependencies(info["node_dependencies"], staging / "node_modules", cancel=cancel, budget=budget)
            _check_cancel(cancel)
            os.rename(staging / "node_modules", target)
    # A completed Node rename must also get its marker even if cancellation
    # arrives now; otherwise a later retry could mistake it for user data.
    if family != "node":
        _check_cancel(cancel)
    saved = {**info, "signature": signature, "copied_python_dependencies": copied_sites, "runtime_dir": str(runtime)}
    _atomic_json(marker, saved)
    return saved


async def _prepare_environment(source, candidate, runner, timeout):
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(prepare_environment, source, candidate, runner, cancel=cancel))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except BaseException:
        cancel.set()
        # Never report cancellation while a background copy can still write.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


# -I -S suppresses PYTHONPATH, user site and .pth/editable-install hooks. Only
# candidate code plus detached dependency bytes are added to the stdlib path.
PYTHON_BOOTSTRAP = """import sys,runpy,json,os
root,sites,runner,targets=json.loads(sys.argv[1])
sys.path[:0]=[root,os.path.join(root,'src')]+sites
os.chdir(root)
print('CANDIDATE_IMPORT_ROOT: '+root, flush=True)
if runner=='pytest':
    import pytest
    raise SystemExit(pytest.main(['--rootdir='+root,'--basetemp='+os.path.join(root,'.mewcode','verification','pytest-tmp')]+targets))
sys.argv=['unittest','discover','-s',targets[0],'-t',root] if len(targets)==1 and os.path.isdir(targets[0]) else ['unittest']+targets
runpy.run_module('unittest',run_name='__main__')
"""


def candidate_diff(baseline, candidate, before, after):
    changed = [key for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]
    pieces = []
    for key in changed[:20]:
        a, b = Path(baseline) / key, Path(candidate) / key
        if (a.exists() and a.stat().st_size > 100_000) or (b.exists() and b.stat().st_size > 100_000):
            pieces.append(f"{key}: large/binary content; inspect artifact hashes")
            continue
        try:
            old = a.read_text(encoding="utf-8").splitlines() if a.exists() else []
            new = b.read_text(encoding="utf-8").splitlines() if b.exists() else []
            pieces.extend(difflib.unified_diff(old, new, fromfile="baseline/" + key, tofile="candidate/" + key, lineterm=""))
        except UnicodeError:
            pieces.append(f"{key}: binary file changed")
    return changed, "\n".join(pieces)[:16000]


async def verify_candidate(context, runner, targets, timeout):
    deadline = asyncio.get_running_loop().time() + timeout
    source, candidate, baseline = (Path(context[key]).resolve(strict=True) for key in ("source", "candidate", "baseline"))
    if Path(context["candidate"]).absolute() != candidate or Path(context["baseline"]).absolute() != baseline:
        raise ValueError("候选或基线根路径已被链接重定向，未启动验证")
    if (runner not in {"pytest", "unittest", "npm_test"} or timeout <= 0):
        raise ValueError("不支持的候选验证器或超时时间")
    if (candidate == source or candidate.is_relative_to(source) or source.is_relative_to(candidate)
            or baseline == candidate or baseline.is_relative_to(candidate) or candidate.is_relative_to(baseline)
            or baseline == source or baseline.is_relative_to(source) or source.is_relative_to(baseline)):
        raise ValueError("验证目录必须是项目之外的独立候选副本")
    if not targets and runner != "npm_test":
        raise ValueError("请指定相关测试文件或测试目录，不自动扫描整个项目")
    for value in targets:
        path = str(value).split("::", 1)[0]
        if not path or Path(path).is_absolute() or path.startswith("-") or any(c in path for c in ";&|<>`$%\"\n\r"):
            raise ValueError("测试目标只能是候选目录中的文件或目录，不能携带命令或选项")
        resolved = (candidate / path).resolve(strict=True)
        if (not resolved.is_relative_to(candidate)
                or any(part in {".mewcode", ".venv", "venv", "node_modules", ".git"} for part in resolved.relative_to(candidate).parts)):
            raise ValueError("测试目标离开候选代码目录")
    original_before, baseline_before = await asyncio.gather(asyncio.to_thread(manifest, source), asyncio.to_thread(manifest, baseline))
    await asyncio.to_thread(manifest, candidate)
    if original_before != baseline_before:
        raise ValueError("原项目已不同于冻结基线，未启动候选测试")
    env_info = await _prepare_environment(source, candidate, runner, max(0, deadline - asyncio.get_running_loop().time()))
    runtime = Path(env_info["runtime_dir"])
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "NODE_PATH", "NODE_OPTIONS", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        env.pop(key, None)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
               COVERAGE_FILE=str(runtime / ".coverage"), XDG_CACHE_HOME=str(runtime / "cache"),
               TMP=str(runtime / "tmp"), TEMP=str(runtime / "tmp"), TMPDIR=str(runtime / "tmp"))
    for name in ("tmp", "cache", "npm-cache", "pytest-tmp"):
        _local_path(candidate, runtime / name).mkdir(exist_ok=True)
    _local_path(candidate, runtime / ".coverage")
    if runner == "pytest" and env_info["python_origin"] == "host_runtime":
        raise ValueError("原项目未找到 .venv/venv；pytest 依赖不可用，请提供匹配的项目环境")
    if runner in {"pytest", "unittest"}:
        argv = [env_info["python"], "-I", "-S", "-B", "-X", "utf8", "-c", PYTHON_BOOTSTRAP,
                json.dumps([str(candidate), env_info["copied_python_dependencies"], runner, targets])]
    elif runner == "npm_test":
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            raise ValueError("npm 不可用；请提供 Node 测试环境，不要反复重跑相同命令")
        if not (candidate / "package.json").is_file():
            raise ValueError("候选目录缺少 package.json")
        if os.name == "nt":
            command = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in [npm, "test", "--", *targets])
            shell, arguments = _shell_command(command)
            argv = [shell, *arguments]
        else:
            argv = [npm, "test", "--", *targets]
        env.update(npm_config_cache=str(runtime / "npm-cache"), npm_config_offline="true", npm_config_audit="false", npm_config_fund="false")
    else:
        raise ValueError("不支持的候选验证器")
    if asyncio.get_running_loop().time() >= deadline:
        raise asyncio.TimeoutError()
    proc = await asyncio.create_subprocess_exec(*argv, cwd=candidate, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name != "nt", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=max(0, deadline - asyncio.get_running_loop().time()))
    except BaseException:
        await _terminate_process_tree(proc)
        raise
    after = await asyncio.to_thread(manifest, candidate)
    changed, diff = candidate_diff(baseline, candidate, baseline_before, after)
    original_after, baseline_after = await asyncio.gather(asyncio.to_thread(manifest, source), asyncio.to_thread(manifest, baseline))
    conflict = original_after != original_before or baseline_after != baseline_before
    return {"runner": runner, "targets": targets, "cwd": str(candidate), "python": env_info["python"],
            "dependency_mode": "detached_copy", "exit_code": proc.returncode,
            "source_unchanged": not conflict, "artifact_hash": content_hash(after),
            "source_check_scope": "project-file manifests; excludes .git, dependency directories and runtime caches; not OS isolation",
            "changed_files": changed, "diff": diff, "output": output.decode(errors="replace")[-24000:]}
