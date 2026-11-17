#!/usr/bin/env python3
"""Run trigger evaluation for a skill description.

Modified from Anthropic skill-creator (Apache-2.0). Detects MewCode LoadSkill
invocations instead of calling the Claude Code CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from scripts.utils import parse_skill_md

from mewcode.skills.runtime import detect_skill_trigger, find_project_root
from mewcode.skills.skill_eval import run_eval as core_run_eval


def run_eval(
    eval_set: list[dict],
    skill_name: str,
    description: str,
    num_workers: int = 1,
    timeout: int = 30,
    project_root: Path | None = None,
    runs_per_query: int = 1,
    trigger_threshold: float = 0.5,
    model: str | None = None,
    detect_fn: Callable[[str], bool] | None = None,
) -> dict:
    del num_workers, timeout, model
    root = Path(project_root) if project_root is not None else find_project_root()
    detector = detect_fn or (
        lambda query: detect_skill_trigger(query, skill_name, str(root))
    )
    return core_run_eval(
        eval_set,
        skill_name=skill_name,
        description=description,
        detect_fn=detector,
        runs_per_query=runs_per_query,
        trigger_threshold=trigger_threshold,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run trigger evaluation for a skill description"
    )
    parser.add_argument("--eval-set", required=True, help="Path to eval set JSON file")
    parser.add_argument("--skill-path", required=True, help="Path to skill directory")
    parser.add_argument("--description", default=None, help="Override description to test")
    parser.add_argument("--num-workers", type=int, default=1, help="Unused (kept for compatibility)")
    parser.add_argument("--timeout", type=int, default=30, help="Unused (kept for compatibility)")
    parser.add_argument("--runs-per-query", type=int, default=3, help="Number of runs per query")
    parser.add_argument("--trigger-threshold", type=float, default=0.5, help="Trigger rate threshold")
    parser.add_argument("--model", default=None, help="Unused (kept for compatibility)")
    parser.add_argument("--verbose", action="store_true", help="Print progress to stderr")
    args = parser.parse_args()

    eval_set = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
    skill_path = Path(args.skill_path)
    if not (skill_path / "SKILL.md").exists():
        print(f"Error: No SKILL.md found at {skill_path}", file=sys.stderr)
        sys.exit(1)

    name, original_description, _content = parse_skill_md(skill_path)
    description = args.description or original_description
    project_root = find_project_root()

    if args.verbose:
        print(f"Evaluating: {description}", file=sys.stderr)

    output = run_eval(
        eval_set=eval_set,
        skill_name=name,
        description=description,
        project_root=project_root,
        runs_per_query=args.runs_per_query,
        trigger_threshold=args.trigger_threshold,
    )

    if args.verbose:
        summary = output["summary"]
        print(
            f"Results: {summary['passed']}/{summary['total']} passed",
            file=sys.stderr,
        )
        for row in output["results"]:
            status = "PASS" if row["pass"] else "FAIL"
            rate_str = f"{row['triggers']}/{row['runs']}"
            print(
                f"  [{status}] rate={rate_str} expected={row['should_trigger']}: "
                f"{row['query'][:70]}",
                file=sys.stderr,
            )

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
