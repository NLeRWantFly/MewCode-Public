from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from mewcode.tools.load_skill import LoadSkill, LoadSkillParams
from mewcode.tools.base import ToolResult

TriggerMap = dict[str, list[bool]]


class RecordingLoadSkill(LoadSkill):
    def __init__(self) -> None:
        super().__init__()
        self.invoked: list[str] = []

    async def execute(self, params: BaseModel) -> ToolResult:
        if isinstance(params, LoadSkillParams):
            self.invoked.append(params.name)
        return await super().execute(params)


def summarize_eval(
    eval_set: list[dict[str, Any]],
    trigger_results: TriggerMap,
    *,
    skill_name: str,
    description: str,
    trigger_threshold: float = 0.5,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for item in eval_set:
        query = item["query"]
        should_trigger = bool(item["should_trigger"])
        triggers = trigger_results.get(query, [])
        runs = len(triggers)
        trigger_rate = (sum(triggers) / runs) if runs else 0.0
        if should_trigger:
            did_pass = trigger_rate >= trigger_threshold
        else:
            did_pass = trigger_rate < trigger_threshold
        results.append(
            {
                "query": query,
                "should_trigger": should_trigger,
                "trigger_rate": trigger_rate,
                "triggers": sum(triggers),
                "runs": runs,
                "pass": did_pass,
            }
        )

    passed = sum(1 for row in results if row["pass"])
    total = len(results)
    return {
        "skill_name": skill_name,
        "description": description,
        "results": results,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
        },
    }


def run_eval(
    eval_set: list[dict[str, Any]],
    *,
    skill_name: str,
    description: str,
    detect_fn: Callable[[str], bool],
    runs_per_query: int = 1,
    trigger_threshold: float = 0.5,
) -> dict[str, Any]:
    trigger_results: TriggerMap = {}
    for item in eval_set:
        query = item["query"]
        trigger_results[query] = [bool(detect_fn(query)) for _ in range(runs_per_query)]
    return summarize_eval(
        eval_set,
        trigger_results,
        skill_name=skill_name,
        description=description,
        trigger_threshold=trigger_threshold,
    )
