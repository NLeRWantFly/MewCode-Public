"""A reviewed, offline workspace write; never an alternative command executor."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from mewcode.tools.base import Tool, ToolResult, active_work_dir


DistributionName = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=200,
    pattern=r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")]


class PreparePythonEnvironmentParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packages: list[DistributionName] = Field(default_factory=list, max_length=32,
        description="Optional installed distribution names. Applicable installed Requires-Dist dependencies are copied offline; extras are unsupported.")

    @field_validator("packages")
    @classmethod
    def reject_duplicate_distribution_aliases(cls, values):
        canonical = [re.sub(r"[-_.]+", "-", value).lower() for value in values]
        if len(canonical) != len(set(canonical)):
            raise ValueError("packages must not contain duplicate distribution names")
        return values


class PreparePythonEnvironment(Tool):
    name = "PreparePythonEnvironment"
    category = "write"
    params_model = PreparePythonEnvironmentParams
    description = (
        "Prepare a missing project .venv using the app's known Python runtime, optionally copying "
        "already-installed distributions and their applicable Requires-Dist dependency closure from "
        "trusted sysconfig package directories. Extras and direct URL dependencies are unsupported; "
        "missing or incompatible dependencies are reported before the environment is created. This is "
        "a separate environment write requiring normal permission review; a read-only command request "
        "does not authorize it. No network, pip, shell, global install, permission changes or overwrite "
        "of existing environments. After preparation, use the ordinary Bash execution boundary to "
        "verify imports. Retry an original probe only if its host receipt proves command_started=false; "
        "never report the business node complete from environment preparation."
    )

    async def execute(self, params: PreparePythonEnvironmentParams) -> ToolResult:
        from mewcode.sandbox.project_python import prepare_project_python
        root = active_work_dir()
        if not root:
            return ToolResult("No bound project; environment was not prepared.", is_error=True)
        # Python cannot cancel a filesystem worker thread. Settle the bounded
        # copy before acknowledging cancellation, so writes never outlive the tool.
        work = asyncio.create_task(asyncio.to_thread(prepare_project_python, root, packages=tuple(params.packages)))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(work)
                break
            except asyncio.CancelledError:
                if work.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return ToolResult(json.dumps(result, ensure_ascii=False), is_error=not result.get("ok"),
                          host_observation={"project_python_preparation": result})
