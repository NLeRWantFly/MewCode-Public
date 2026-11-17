"""Agent-accessible diagnostics for the host-authorized recovery service."""
from __future__ import annotations

import asyncio
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mewcode.tools.base import Tool, ToolResult, ToolRiskProfile, active_work_dir


class RuntimeDoctorParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["inspect", "recover"] = Field(default="inspect", description=(
        "Inspect the native boundary or make one host-authorized repair attempt. "
        "Recovery only restores lost enablement for the exact previously enabled, attested bundle."))


class RuntimeDoctor(Tool):
    name = "RuntimeDoctor"
    description = (
        "Diagnose a native execution blocker and recover when a prior host opt-in permits it. "
        "Does not execute commands, change permission mode, grant scope, install software, "
        "create acceptance evidence or dismiss Windows UAC. After recovery, retry a command "
        "only when its host receipt proves command_started=false; otherwise reconcile first."
        " Inspect includes workspace_transactions with exact pending IDs, states and cleanup-evidence diagnostics; "
        "read those host facts directly instead of asking the user for recovery-panel screenshots. "
        "Recover restores runtime enablement only, not missing transaction cleanup evidence."
    )
    params_model = RuntimeDoctorParams
    category = "read"

    def permission_risk(self, arguments):
        return ToolRiskProfile(side_effect="workspace" if arguments.get("action") == "recover" else "none",
                               target_type="host_runtime")

    async def execute(self, params: RuntimeDoctorParams) -> ToolResult:
        from mewcode.sandbox.native_recovery import diagnose_native_runtime, recover_native_runtime
        operation = recover_native_runtime if params.action == "recover" else diagnose_native_runtime
        result = await asyncio.to_thread(operation)
        from mewcode.permissions.network_policy import active_policy_owner
        checker = active_policy_owner()
        native = checker is None or checker.uses_native_commands()
        result["execution_backend"] = "native" if native else "host_reviewed"
        result["native_ready"] = result["ready"]
        result["commands_ready"] = result["ready"] if native else True
        if native and checker is not None:
            from mewcode.permissions.network_policy import policy_for
            if policy_for(checker).enabled:
                from mewcode.sandbox.srt_runtime import runtime_status
                result["network_runtime"] = await asyncio.to_thread(runtime_status)
                result["commands_ready"] = result["network_runtime"]["ready"]
        from mewcode.runtime_revision import runtime_revision
        result["application_revision"] = await asyncio.to_thread(runtime_revision)
        if active_work_dir():
            from pathlib import Path
            from mewcode.sandbox.transaction_registry import workspace_write_block, workspace_command_block
            blocker = await asyncio.to_thread(workspace_write_block, Path(active_work_dir()))
            command_blocker = (await asyncio.to_thread(workspace_command_block, Path(active_work_dir()))) if native else blocker
            result.update(workspace_ready=not blocker, workspace_blocker=blocker,
                          commands_ready=result["commands_ready"] and not command_blocker,
                          command_blocker=command_blocker)
            from mewcode.sandbox.transaction_diagnostics import workspace_transactions
            result["workspace_transactions"] = await asyncio.to_thread(workspace_transactions, Path(active_work_dir()))
            from mewcode.sandbox.project_python import inspect_project_python
            result["project_python"] = await asyncio.to_thread(inspect_project_python, active_work_dir())
        return ToolResult(json.dumps(result, ensure_ascii=False),
                          is_error=params.action == "recover" and not result["commands_ready"],
                          host_observation={"runtime_recovery": result})
