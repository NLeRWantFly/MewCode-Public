"""Recompute the collaboration change inventory against the frozen worktree."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default=".tmp/collab-mode-20261012/source-baseline")
    parser.add_argument("--output", default="docs/collab-refactor-baseline-20261012.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    baseline = root / args.baseline
    prefixes = ("mewcode/orchestration/", "mewcode/experts/", "mewcode/kernel/")
    explicit = {
        "mewcode/expert_group.py", "mewcode/expert_runs.py", "mewcode/expert_web.py",
        "mewcode/tools/task_design.py", "mewcode/tools/workflow_blocks.py", "mewcode/tools/submit_handoff.py",
        "frontend/task_design.py", "frontend/orch_drafts.py", "frontend/server.py",
        "frontend/static/task-design.js", "frontend/static/task-design.css", "frontend/static/orch-generate.js",
    }
    paths = set(explicit)
    for base in (root, baseline):
        for prefix in prefixes:
            paths.update(path.relative_to(base).as_posix() for path in (base / prefix).glob("*.py"))
    files = []
    for relative in sorted(paths):
        row = {"path": relative}
        contents = []
        for key, path in (("before", baseline / relative), ("after", root / relative)):
            data = path.read_bytes() if path.is_file() else None
            contents.append(data)
            row[key] = {"lines": len(data.splitlines()) if data is not None else 0,
                        "sha256": hashlib.sha256(data).hexdigest() if data is not None else None}
        row["classification"] = "retained" if contents[0] == contents[1] else "added" if contents[0] is None else "removed" if contents[1] is None else "adapted"
        row["line_delta"] = row["after"]["lines"] - row["before"]["lines"]
        row["owner"] = "collaborative" if ("/collab" in relative or relative.rsplit("/", 1)[-1] in {"acceptance.py", "contracts.py", "constraints.py", "ask_gateway.py"}) else "shared_or_classic"
        files.append(row)
    total_before = sum(row["before"]["lines"] for row in files)
    total_after = sum(row["after"]["lines"] for row in files)
    result = {
        "schema": "mewcode.collaboration.inventory.v1", "baseline": args.baseline,
        "baseline_note": "Frozen uncommitted worktree before implementation; not a clean HEAD baseline. Existing changes were preserved.",
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "recompute": "MEWCODE_OFFLINE_TEST=1 .venv/Scripts/python.exe scripts/collaboration_inventory.py",
        "files": files,
        "totals": {"files": len(files), "before_lines": total_before, "after_lines": total_after,
                   "delta": total_after-total_before, "reduction_percent": round(100*(total_before-total_after)/total_before, 2)},
        "callers": {
            "design": ["frontend/task_design.py:start/drive", "tools/task_design.py:AdvanceTaskDesign", "orchestration/workflow_blocks.py:read/submit"],
            "runtime": ["expert_web.py:run_expert_chat", "expert_group.py:run", "orchestration/collab_runtime.py:run", "expert_web.py:run_node/run_role"],
            "classic": ["expert_group.py:_run_graph", "kernel/escalation.py", "experts/loop_runtime.py"],
            "storage": ["orchestration/draft_store.py", "expert_runs.py:ExpertRunStore", "kernel/events.py:EventJournal"],
        },
        "canvas_coupling": {
            "frontend/orch_drafts.py:draft_graph": "Attaches task identity and source-linked collaboration specification before preview/save.",
            "orchestration/convert.py": "Preserves selected mode and collaboration metadata across graph conversion.",
            "expert_web.py:check_saved_revision": "Existing saved-plan guard remains; runtime requirement changes go to the bound ledger.",
        },
        "test_behavior_mapping": {
            "tests/test_collaboration_mode.py": "Mode identity, ledger authority, acceptance, independent review, restart and stale deliveries.",
            "tests/test_collaboration_integration.py": "Coordinator budgets, questions, revision API, loop/batch, milestone/final integration, migration and start binding.",
            "tests/test_collaboration_web.py": "Real web startup/stage/Agent permission/receipt paths with offline actors and synthetic external command environment.",
            "tests/orch_task_design.cjs": "Both mode choices, revision payload/focus preservation, classic controls and scope errors.",
            "existing tests": "All prior behavior tests retained; frozen baseline/final per-file outputs record failures and timeouts explicitly.",
        },
        "retired_tests": [], "removed_protocols": [],
        "size_target": "No reduction target claimed: user explicitly retained both modes; classic scheduler and shared permission/recovery code remain.",
    }
    destination = root / args.output
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["totals"]))


if __name__ == "__main__":
    main()
