from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from mewcode.tools.base import Tool, ToolResult
from mewcode.teams.store_v2 import TeamStateError


class TeamCommandParams(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command: str
    arguments: dict[str, Any]


class TeamCommandTool(Tool):
    name = 'TeamCommand'
    description = ('Run an explicit host-authorized v2 command on this team run. Commands: activate, register_input, amend_plan, set_member_state, '
                   'accept_input, review, resume, revise, resolve_usage, grant_export, revoke_artifact, suspend, take_over, '
                   'cancel_run, finalize, close_team, send_message, ack_message. Arguments must include command_id and '
                   'current control_epoch; task updates also require exact revision/state_version fields per command. '
                   'Review requires exact handoff_id, expected_revision, accepted and criterion scores with evidence. '
                   'amend_plan requires definition with a newer spec version, approved=true, reason and expected_version; existing contracts/roles/edges and the budget grant cannot expand. '
                   'Use TeamRoster to read bindings, member availability and durable inbox messages. '
                   'No identity arguments, generic status writes, model calls or automatic retries are allowed.')
    params_model = TeamCommandParams
    category = 'command'
    is_concurrency_safe = True

    def __init__(self, scope):
        self.scope = scope

    async def execute(self, params):
        try:
            return ToolResult(output=json.dumps(self.scope.command(params.command, params.arguments), ensure_ascii=False))
        except (ValueError, TypeError, TeamStateError) as exc:
            return ToolResult(output=str(exc), is_error=True)
