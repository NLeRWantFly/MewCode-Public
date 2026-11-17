"""Freeze and run the T81 frontend test baseline without touching user stores.

Run with the repository venv Python. ``--collect-only`` writes the frozen file
selection; the next invocation reuses that selection and checks source hashes.
Browser modules are syntax checked but never reported as executed scenarios.
"""
from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp/workbench-20261013"
SELECTION_RULE = (
    "All tests/*.cjs; test_*.py recursively under tests with an import "
    "from/of frontend (including subprocess snippets) or literal/composed frontend/static reference; additionally "
    "test_collaboration_web.py, test_task_design_*.py, test_remote_mewcode_collab.py"
)
NODE_GUARD = r"""'use strict';
const net = require('node:net');
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  const normal = Array.isArray(args[0]) ? args[0] : args;
  const first = normal[0];
  const host = first && typeof first === 'object' ? first.host :
    typeof normal[1] === 'string' ? normal[1] : undefined;
  if (host && !['127.0.0.1', 'localhost', '::1'].includes(host)) {
    throw Error('Offline baseline blocked an external network connection');
  }
  return connect.apply(this, args);
};
"""
ACTIVE: set[subprocess.Popen] = set()
ACTIVE_LOCK = threading.Lock()
STOP = threading.Event()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def enumerate_tests() -> list[dict]:
    selected = []
    for path in sorted((ROOT / "tests").glob("*.cjs")):
        source = path.read_text(encoding="utf-8-sig")
        module_only = bool(re.search(r"module\.exports\s*=", source)) and "require.main" not in source
        selected.append({
            "file": relative(path), "language": "node", "sha256": sha256(path),
            "selection_reason": "tests/*.cjs",
            "execution_kind": "browser_module" if module_only else "node_program",
        })
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
        direct_import = any(
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "frontend"
            or isinstance(node, ast.Import)
            and any(alias.name.split(".")[0] == "frontend" for alias in node.names)
            for node in ast.walk(tree)
        )
        reasons = []
        if direct_import or re.search(r"^\s*(?:from\s+frontend\b|import\s+frontend\b)", source, re.MULTILINE):
            reasons.append("direct frontend import")
        static_reference = "frontend/static" in source.replace("\\", "/") or bool(
            re.search(r'''["']frontend["']\s*[/,]\s*["']static["']''', source)
        )
        if static_reference:
            reasons.append("frontend/static reference")
        if path.name in {"test_collaboration_web.py", "test_remote_mewcode_collab.py"} or path.name.startswith("test_task_design_"):
            reasons.append("explicit T81 scope")
        if reasons:
            selected.append({"file": relative(path), "language": "python", "sha256": sha256(path),
                             "selection_reason": "; ".join(reasons), "execution_kind": "pytest"})
    return selected


def uses_node(path: Path, visited: set[Path] | None = None) -> bool:
    """Include Python wrappers/Playwright drivers when repairing a Node runner."""
    visited = set() if visited is None else visited
    if path in visited or not path.is_file():
        return False
    visited.add(path)
    source = path.read_text(encoding="utf-8-sig")
    if "playwright" in source or re.search(
        r"subprocess\.(?:run|Popen|check_output|check_call)|create_subprocess_exec", source
    ) and re.search(r'''\.cjs|electron|\[\s*["']node["']''', source):
        return True
    for node in ast.walk(ast.parse(source)):
        modules = [node.module] if isinstance(node, ast.ImportFrom) else (
            [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
        for module in modules:
            if module and module.startswith("tests.") and uses_node(ROOT / (module.replace(".", "/") + ".py"), visited):
                return True
    return False


def isolated_env(folder: Path, node_guard: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Reuse installed browser executables, never the user's browser profile.
    browser_cache = Path(env.get("LOCALAPPDATA", "")) / "ms-playwright"
    if "PLAYWRIGHT_BROWSERS_PATH" not in env and browser_cache.is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_cache)
    for key in list(env):
        if key.endswith(("_API_KEY", "_ACCESS_TOKEN")) or key in {
            "ANTHROPIC_AUTH_TOKEN", "MEWCODE_DESKTOP_TOKEN", "PYTEST_ADDOPTS",
            "ELECTRON_RUN_AS_NODE", "NODE_OPTIONS",
        }:
            env.pop(key, None)
    locations = {
        "HOME": "home", "USERPROFILE": "home", "APPDATA": "appdata",
        "LOCALAPPDATA": "localappdata", "TMP": "tmp", "TEMP": "tmp",
        "XDG_CONFIG_HOME": "config", "XDG_CACHE_HOME": "cache",
        "MEWCODE_USER_DATA_DIR": "userdata",
    }
    for key, name in locations.items():
        directory = folder / name
        directory.mkdir(parents=True, exist_ok=True)
        env[key] = str(directory)
    env.update({"MEWCODE_OFFLINE_TEST": "1", "MEWCODE_DESKTOP_PROFILE_ISOLATED": "1",
                "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                "NODE_OPTIONS": '--require "' + node_guard.as_posix() + '"'})
    return env


def terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=20, check=False)
    if process.poll() is None:
        process.kill()


def pytest_counts(xml: Path) -> tuple[dict, list[dict]]:
    counts = dict(passed=0, failed=0, skipped=0, errors=0, timeout=0)
    cases = []
    if not xml.exists():
        return counts, cases
    try:
        root = ET.parse(xml).getroot()
    except ET.ParseError:
        return counts, cases
    for case in root.iter("testcase"):
        status = "passed"
        detail = None
        for tag, kind in (("failure", "failed"), ("error", "errors"), ("skipped", "skipped")):
            element = case.find(tag)
            if element is not None:
                status, detail = kind, element.get("message", "")
                break
        counts[status] += 1
        cases.append({"classname": case.get("classname"), "name": case.get("name"),
                      "status": status, "message": detail})
    return counts, cases


def run_one(item: dict, output: Path, python: str, node: str, node_guard: Path,
            python_timeout: int, node_timeout: int, attempt: int = 1) -> dict:
    if STOP.is_set():
        return {**item, "status": "not_run", "reason": "runner interrupted"}
    stem = Path(item["file"]).name.replace(".", "-")
    folder = output / "tests" / (f"attempt-{attempt}" if attempt > 1 else "") / stem
    folder.mkdir(parents=True, exist_ok=True)
    log, xml = folder / "output.log", folder / "junit.xml"
    env = isolated_env(folder / "isolation", node_guard)
    module_only = item["execution_kind"] == "browser_module"
    if item["language"] == "python":
        command = [python, "-m", "pytest", item["file"], "-q", "-ra", "--tb=short",
                   "-o", "addopts=", "-p", "no:cacheprovider", "--junitxml", str(xml),
                   "--basetemp", str(folder / "pytest-temp")]
        timeout = python_timeout
    else:
        command = [node] + (["--check"] if module_only else []) + [item["file"]]
        timeout = node_timeout
    started, monotonic = utc_now(), time.monotonic()
    timed_out, error, exit_code = False, None, None
    with log.open("wb") as stream:
        try:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            with ACTIVE_LOCK:
                ACTIVE.add(process)
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_tree(process)
                exit_code = process.wait(timeout=20)
                stream.write(f"\nBASELINE TIMEOUT after {timeout}s\n".encode())
            finally:
                with ACTIVE_LOCK:
                    ACTIVE.discard(process)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            stream.write(("\nBASELINE RUNNER ERROR: " + error + "\n").encode("utf-8"))
    text = log.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    counts, cases = pytest_counts(xml)
    counts_complete = xml.exists() if item["language"] == "python" else False
    count_basis = "junit testcases" if counts_complete else "not reported by program"
    if item["language"] == "node":
        tap = dict(re.findall(r"^(?:#|ℹ) (pass|fail|skipped|cancelled) (\d+)\s*$", text, re.MULTILINE))
        if tap:
            counts_complete = True
            counts.update(passed=int(tap.get("pass", 0)), failed=int(tap.get("fail", 0)),
                          skipped=int(tap.get("skipped", 0)), errors=int(tap.get("cancelled", 0)))
            count_basis = "node reporter summary"
    counts["timeout"] = int(timed_out)
    status = "timeout" if timed_out else "error" if error else "passed" if exit_code == 0 else "failed"
    if module_only and status == "passed":
        status = "requires_browser_harness"
    failure_ids = re.findall(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$", text, re.MULTILINE)
    summary = next((line for line in reversed(lines) if re.search(r"\d+ (?:passed|failed|error|skipped)", line)), lines[-1] if lines else "(no output)")
    if item["language"] == "node":
        failure_ids = re.findall(r"^test at (.+)$", text, re.MULTILINE)
        if tap:
            summary = "; ".join(f"{key} {value}" for key, value in tap.items())
    return {**item, "status": status, "command": command,
            "command_display": subprocess.list2cmdline(command), "cwd": str(ROOT),
            "started_at": started, "duration_seconds": round(time.monotonic() - monotonic, 3),
            "timeout_seconds": timeout, "exit_code": exit_code, "counts": counts,
            "count_basis": count_basis, "case_counts_complete": counts_complete,
            "case_results": cases, "failure_ids": failure_ids,
            "summary_line": summary, "last_output_lines": lines[-8:], "runner_error": error,
            "execution_note": "Syntax check only; exported browser scenario/assertions were not executed" if module_only else None,
            "log": relative(log), "log_sha256": sha256(log),
            "junit_xml": relative(xml) if xml.exists() else None,
            "junit_sha256": sha256(xml) if xml.exists() else None}


def aggregate(results: list[dict]) -> dict:
    statuses: dict[str, int] = {}
    counts = dict(passed=0, failed=0, skipped=0, errors=0, timeout=0)
    for result in results:
        statuses[result["status"]] = statuses.get(result["status"], 0) + 1
        for key in counts:
            counts[key] += result.get("counts", {}).get(key, 0)
    return {"completed_files": len(results), "file_statuses": statuses, "reported_case_counts": counts,
            "note": "Case totals exclude programs without case counts and unexecuted browser modules."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--retry-language", choices=("node", "python"),
                        help="Rerun one language after completion, preserving prior attempt evidence")
    parser.add_argument("--retry-reason", default="explicit language retry")
    parser.add_argument("--retry-node-dependents", action="store_true",
                        help="Also retry Python wrappers and Playwright affected by a Node launch error")
    parser.add_argument("--extend-selection", action="store_true",
                        help="Append newly discovered files while preserving the original frozen hashes")
    parser.add_argument("--workers", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--python-timeout", type=int, default=180)
    parser.add_argument("--node-timeout", type=int, default=60)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".tmp"):
        parser.error("output must be inside the repository .tmp directory")
    if not 1 <= args.python_timeout <= 180 or not 1 <= args.node_timeout <= 60:
        parser.error("timeouts must be positive and at most 180s (Python) / 60s (Node)")
    output.mkdir(parents=True, exist_ok=True)
    selection_path, result_path = output / "test-selection.json", output / "test-baseline.json"
    if selection_path.exists():
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        if args.extend_selection:
            existing = {item["file"] for item in selection["files"]}
            added = [item for item in enumerate_tests() if item["file"] not in existing]
            selection["files"].extend(added)
            selection["rule"] = SELECTION_RULE
            selection.setdefault("amendments", []).append({"at": utc_now(), "added_files": added})
            write_json(selection_path, selection)
    else:
        selection = {"frozen_at": utc_now(), "rule": SELECTION_RULE, "files": enumerate_tests()}
        write_json(selection_path, selection)
    files = selection["files"]
    invalid = [item["file"] for item in files if not (ROOT / item["file"]).is_file()
               or sha256(ROOT / item["file"]) != item["sha256"]]
    if invalid:
        parser.error("frozen test sources changed: " + ", ".join(invalid))
    print(json.dumps({"selected_files": len(files), "node": sum(x["language"] == "node" for x in files),
                      "python": sum(x["language"] == "python" for x in files),
                      "browser_modules": sum(x["execution_kind"] == "browser_module" for x in files),
                      "selection": relative(selection_path), "runner": relative(Path(__file__).resolve())}), flush=True)
    if args.collect_only:
        return 0
    previous = None
    if args.retry_language:
        if not result_path.exists():
            parser.error("--retry-language requires an existing completed result")
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        if not previous.get("complete"):
            parser.error("previous run is not complete; do not run overlapping batches")
    elif result_path.exists():
        parser.error("result already exists; choose a new --output to avoid overwriting baseline evidence")
    python = str(ROOT / ".venv/Scripts/python.exe") if os.name == "nt" else sys.executable
    node = shutil.which("node")
    if not node:
        parser.error("node is unavailable")
    guard = output / "offline-node-guard.cjs"
    guard.write_text(NODE_GUARD, encoding="utf-8")
    metadata = {"schema_version": 1, "started_at": utc_now(), "selection": selection,
                "runner_sha256": sha256(Path(__file__)), "python": python, "node": node,
                "workers": args.workers, "environment": {"MEWCODE_OFFLINE_TEST": "1",
                "profile": "fresh per-file workspace directories; Python conftest also isolates Path.home",
                "network": "Python conftest and Node preload block external socket connections"},
                "results": [], "complete": False}
    attempt = 1
    run_files = files
    if previous:
        attempt = previous.get("attempt", 1) + 1
        completed_files = {item["file"] for item in previous["results"]}
        retry_files = {item["file"] for item in files if item["language"] == args.retry_language
                       or item["file"] not in completed_files
                       or args.retry_node_dependents and item["language"] == "python" and uses_node(ROOT / item["file"])}
        if args.retry_node_dependents:
            retry_files.update(item["file"] for item in previous["results"]
                               if "offline-node-guard.cjs" in (ROOT / item["log"]).read_text(encoding="utf-8", errors="replace"))
        run_files = [item for item in files if item["file"] in retry_files]
        metadata["results"] = [item for item in previous["results"] if item["file"] not in retry_files]
        metadata["superseded_attempts"] = previous.get("superseded_attempts", []) + [{
            "attempt": previous.get("attempt", 1), "runner_sha256": previous["runner_sha256"],
            "reason": args.retry_reason,
            "started_at": previous["started_at"], "finished_at": previous.get("finished_at"),
            "results": [item for item in previous["results"] if item["file"] in retry_files],
        }]
        metadata["retained_results_runner_sha256"] = previous["runner_sha256"]
    metadata["attempt"] = attempt
    write_json(result_path, metadata)
    pool = ThreadPoolExecutor(max_workers=args.workers)
    try:
        futures = [pool.submit(run_one, item, output, python, node, guard,
                               args.python_timeout, args.node_timeout, attempt) for item in run_files]
        for future in as_completed(futures):
            result = future.result()
            metadata["results"].append(result)
            metadata["results"].sort(key=lambda row: row["file"])
            metadata["summary"] = aggregate(metadata["results"])
            write_json(result_path, metadata)
            print(f"[{len(metadata['results'])}/{len(files)}] {result['status']}: {result['file']}", flush=True)
    except BaseException:
        STOP.set()
        with ACTIVE_LOCK:
            for process in list(ACTIVE):
                terminate_tree(process)
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    metadata.update(complete=True, finished_at=utc_now())
    metadata["verification"] = {"selected_files": len(files), "result_files": len(metadata["results"]),
                                "all_selected_recorded": {x["file"] for x in files} == {x["file"] for x in metadata["results"]}}
    write_json(result_path, metadata)
    print(json.dumps(metadata["summary"], ensure_ascii=False), flush=True)
    return 0 if metadata["verification"]["all_selected_recorded"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
