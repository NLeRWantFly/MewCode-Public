"""stdio MCP entry point for Codex <-> Grok Build coordination."""

from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .coord_protocol import (
    HEARTBEAT_INTERVAL_SECONDS,
    LEASE_SECONDS,
    MAX_EXECUTION_TURNS,
    MAX_RETRIES,
    NO_PROGRESS_LIMIT,
    PROTOCOL_VERSION,
    ROLE_DEFINITIONS,
    APPROVAL_MODEL_ID,
    EXTERNAL_EXECUTOR_MODEL_ID,
    CoordinationError,
)
from .coord_store import CoordinationStore

MCP_SERVER_NAME = "mewcode-coordination"
MCP_INSTRUCTIONS = f"""Local Codex-Grok coordination broker. Use only coordination tools: register agents, sync/read capability snapshots, inspect executor readiness, create/read tasks, exchange messages, and close verified tasks. The always-on local worker exclusively claims, updates, and submits executor tasks; never call those lifecycle operations through MCP. The broker provides no shell/file/browser/model execution. Every task must declare explicit tool/skill/path allowlists, write mode, capability hash, acceptance criteria, max turns, and deadline. Undeclared permissions are denied. Approval is host-managed by {APPROVAL_MODEL_ID}; this is a route label and must never be sent as a provider model ID. Tasks declaring run_terminal_cmd are given at least two Grok turns (and never more than the protocol cap of three) so the tool call and its result can both be observed. The only submodel is the external Grok executor identity grok-executor-01 with model {EXTERNAL_EXECUTOR_MODEL_ID}; the broker never substitutes a Codex model. Codex trigger: when the user sends exactly 协同, enable collaboration mode for the next task without starting a model call; when the user sends 协同: or 协同： followed by a task, inspect executor readiness, create the bounded executor task only when ready, and route evidence to the verifier."""


def project_root() -> Path:
    expected = Path(__file__).resolve().parents[2]
    configured = os.environ.get("MEWCODE_BRIDGE_ROOT")
    if configured and Path(configured).resolve() != expected:
        raise RuntimeError("MEWCODE_BRIDGE_ROOT must point to the fixed MewCode project")
    return expected


def token_file(root: Path) -> Path:
    configured = os.environ.get("MEWCODE_BRIDGE_TOKEN_FILE")
    value = Path(configured) if configured else root / ".mewcode" / "coordination" / ".bridge-token"
    value = value.resolve()
    allowed_parent = (root / ".mewcode" / "coordination").resolve()
    if allowed_parent not in value.parents:
        raise RuntimeError("MEWCODE_BRIDGE_TOKEN_FILE must stay inside .mewcode/coordination")
    value.parent.mkdir(parents=True, exist_ok=True)
    if not value.exists():
        value.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    if not value.read_text(encoding="utf-8").strip():
        raise RuntimeError("coordination token file is empty")
    return value


def state_directory(root: Path) -> Path:
    configured = os.environ.get("MEWCODE_BRIDGE_STATE_DIR")
    value = Path(configured).resolve() if configured else (root / ".mewcode" / "coordination").resolve()
    allowed_parent = (root / ".mewcode" / "coordination").resolve()
    if value != allowed_parent and allowed_parent not in value.parents:
        raise RuntimeError("MEWCODE_BRIDGE_STATE_DIR must stay inside .mewcode/coordination")
    value.mkdir(parents=True, exist_ok=True)
    return value


def default_store() -> CoordinationStore:
    root = project_root()
    token_file(root)
    state_dir = state_directory(root)
    return CoordinationStore(state_dir / "coordination.sqlite3", state_dir / "capabilities")


def build_server(store: CoordinationStore | None = None, *, team_host=None) -> FastMCP:
    state = store
    owns_team_host = False

    def get_store() -> CoordinationStore:
        nonlocal state
        if state is None:
            state = default_store()
        return state

    if team_host is None and store is None:
        from mewcode.teams.host import TeamHost
        from mewcode.config import ConfigError, load_config
        try:
            provider_configs = tuple(load_config().providers)
        except (OSError, ValueError, ConfigError):
            provider_configs = ()
        candidate = TeamHost(project_root(), provider_configs=provider_configs)
        if candidate.enabled:
            team_host = candidate
            owns_team_host = True

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            if owns_team_host:
                await team_host.aclose()
            if store is None and state is not None:
                state.close()

    instructions = MCP_INSTRUCTIONS
    if team_host is not None and team_host.enabled:
        instructions += ('\nOpt-in Team v2 extension: the legacy coordination-only restrictions above still apply to coord_* worker tasks. '
                         'coord_team_v2_execute and coord_team_v2_interpret are separate explicit, budgeted host execution entry points for user-authorized Team v2 runs. '
                         'Inspection, draft, and configuration tools do not start inference. Stop a host job with coord_team_v2_stop_execution. '
                         'These product model routes do not override AGENTS.md development delegation restrictions; never use them to bypass the external Grok-only development policy.')
    mcp = FastMCP(MCP_SERVER_NAME, instructions=instructions, log_level="WARNING", lifespan=lifespan)
    if team_host is not None and team_host.enabled:
        @mcp.tool()
        def coord_team_v2_status() -> dict[str, Any]:
            """Read opt-in team status. This never launches or probes a model."""
            return team_host.status()

        @mcp.tool()
        def coord_team_v2_runs() -> list[dict[str, Any]]:
            """List team runs accessible to this host principal."""
            return team_host.list_runs()

        @mcp.tool()
        def coord_team_v2_snapshot(run_id: str) -> dict[str, Any]:
            """Read exact contracts, versions, receipts, review and recovery state."""
            return team_host.snapshot(run_id)

        @mcp.tool()
        def coord_team_v2_draft(definition: dict[str, Any], name: str, objective: str, budget_ref: str, command_id: str) -> dict[str, Any]:
            """Compile and save an explicit typed team draft; never spawn members."""
            return team_host.draft(definition=definition, name=name, objective=objective, budget_ref=budget_ref, command_id=command_id)

        @mcp.tool()
        def coord_team_v2_activation_plan(run_id: str) -> dict[str, Any]:
            """Read host-attested configured (or legacy Grok) bindings and budgets before activation."""
            return team_host.activation_plan(run_id)

        @mcp.tool()
        def coord_team_v2_command(run_id: str, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            """Explicit scoped host command. Requires command_id and per-command current epoch/version.

            Supports activate/register_input/amend_plan/set_member_state/accept_input/review/resume/revise/resolve_usage,
            grant_export/revoke_artifact/suspend/take_over/cancel_run/finalize/close_team,
            send_message/ack_message; initialize_mission/configure_coordination/review_goal,
            configure_deployment; propose_plan/apply_plan/dismiss_plan;
            submit_intervention/apply_intervention/dismiss_intervention;
            open_dispute/add_dispute_evidence/verify_dispute_evidence/record_dispute_round/resolve_dispute/reopen_dispute;
            grant_resource/revoke_resource/reconcile_tool/recover_coordinator/resolve_coordinator_usage.
            No claim, heartbeat, submit, arbitrary executable, credential or model execution.
            Review must contain accepted, exact expected_revision and all scores with evidence.
            """
            return team_host.command(run_id, command, arguments)

        @mcp.tool()
        def coord_team_v2_goal_draft(objective: str, criteria: list[str], command_id: str,
                                      constraints: list[str] | None = None, domain: str = 'software', name: str | None = None) -> dict[str, Any]:
            """Save a goal contract with an explicitly labeled host-template starter plan; no model call."""
            return team_host.draft_goal(objective=objective, criteria=criteria, constraints=constraints or [], domain=domain, name=name, command_id=command_id)

        @mcp.tool()
        def coord_team_v2_models() -> list[dict[str, Any]]:
            """Read installed model routes, actual adapter presence and declared limits; never probe an API."""
            return team_host.responsibility.pool.available(team_host.actor)

        @mcp.tool()
        def coord_team_v2_events(run_id: str, after: int = 0, limit: int = 100) -> dict[str, Any]:
            """Incrementally read scoped causal events; persist next_cursor for reconnection."""
            return team_host.responsibility.views.events(team_host.actor, run_id, after=after, limit=limit)

        @mcp.tool()
        def coord_team_v2_task_history(task_id: str, before_revision: int | None = None, limit: int = 10) -> dict[str, Any]:
            """Read contracts, input versions, public results and tool/review evidence; never private reasoning."""
            return team_host.responsibility.views.task_history(team_host.actor, task_id, before_revision=before_revision, limit=limit)

        @mcp.tool()
        async def coord_team_v2_execute(run_id: str, control_epoch: int, command_id: str, max_steps: int = 16) -> dict[str, Any]:
            """Explicitly start a bounded host job ONLY for a user-authorized autonomous run.

            Sends scoped inputs to already-selected installed models and spends the
            existing grant. Never call merely to inspect progress or test readiness.
            No route is inferred or substituted. Missing independent checks block closure.
            """
            return team_host.responsibility.start(run_id, control_epoch=control_epoch, max_steps=max_steps, command_id=command_id)

        @mcp.tool()
        async def coord_team_v2_stop_execution(run_id: str, job_id: str, control_epoch: int, command_id: str) -> dict[str, Any]:
            """Explicitly stop one exact host job, preserving task history and unresolved provider/tool outcomes."""
            return await team_host.responsibility.stop(run_id, job_id=job_id, control_epoch=control_epoch, command_id=command_id)

        @mcp.tool()
        async def coord_team_v2_interpret(run_id: str, intervention_id: str, control_epoch: int, command_id: str) -> dict[str, Any]:
            """Explicitly spend the selected coordinator budget to interpret one original user message.

            Requires an installed, granted coordinator route. Control changes
            remain pending confirmation; quoted/retrieved text never grants authority.
            """
            from mewcode.teams.intervention_interpreter import InterventionInterpreter
            return await InterventionInterpreter(team_host.responsibility.coordinator).interpret(run_id, intervention_id=intervention_id,
                                                       control_epoch=control_epoch, command_id=command_id)

        @mcp.tool()
        def coord_team_v2_artifact(version_id: str) -> dict[str, Any]:
            """Read an authorized immutable artifact by exact version, never a workspace path."""
            return team_host.runtime.artifact(team_host.actor, version_id)

    @mcp.tool()
    def coord_protocol() -> dict[str, Any]:
        """Return protocol version, fixed roles, states, and safety limits."""
        from .coord_protocol import TaskState, WriteMode

        return {
            "protocol_version": PROTOCOL_VERSION,
            "roles": ROLE_DEFINITIONS,
            "task_states": [item.value for item in TaskState],
            "write_modes": [item.value for item in WriteMode],
            "limits": {
                "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
                "lease_seconds": LEASE_SECONDS,
                "max_execution_turns": MAX_EXECUTION_TURNS,
                "max_retries": MAX_RETRIES,
                "no_progress_lease_cycles": NO_PROGRESS_LIMIT,
            },
            "broker_boundary": "coordination_only; no shell/file/browser/model execution",
            "approval_contract": {
                "provider_id": "codex-host",
                "model_id": APPROVAL_MODEL_ID,
                "invocation_boundary": "host-managed approval route; broker never invokes it",
            },
            "external_model_contract": {
                "provider_id": "grok-build",
                "model_id": EXTERNAL_EXECUTOR_MODEL_ID,
                "adapter": "mewcode.mcp.external_model.GrokCLIInvoker",
                "invocation_boundary": "explicit external runner only; broker never auto-starts a model",
                "read_only_defaults": {
                    "reasoning_effort": "high",
                    "max_turns": 1,
                    "no_subagents": True,
                    "permission_mode": "plan",
                    "timeout_seconds": 120,
                },
                "terminal_task_constraints": {
                    "tool": "run_terminal_cmd",
                    "min_max_turns": 2,
                    "max_max_turns": MAX_EXECUTION_TURNS,
                },
            },
            "trigger": {
                "word": "协同",
                "standalone": "enable collaboration mode for the next task without starting a model call",
                "inline": "协同: <task> starts a bounded Grok executor task",
            },
        }

    @mcp.tool()
    def coord_register_agent(
        agent_id: str,
        model_id: str,
        role: str,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return get_store().register_agent(agent_id=agent_id, model_id=model_id, role=role, session_id=session_id, metadata=metadata)

    @mcp.tool()
    def coord_sync_capabilities(
        agent_id: str,
        capabilities: dict[str, Any],
        source: str | None = None,
    ) -> dict[str, Any]:
        return get_store().sync_capabilities(agent_id=agent_id, manifest=capabilities, source=source)

    @mcp.tool()
    def coord_get_capabilities(agent_id: str | None = None) -> dict[str, Any]:
        value = get_store().get_capabilities(agent_id)
        if agent_id:
            return {"capabilities": value, "count": 1}
        return {"capabilities": value, "count": len(value)}

    @mcp.tool()
    def coord_get_executor_status(agent_id: str = "grok-executor-01") -> dict[str, Any]:
        """Return the worker's current capability and authentication readiness."""
        agent = get_store().get_agent(agent_id)
        metadata = agent.get("metadata_json")
        if isinstance(metadata, str):
            import json

            metadata = json.loads(metadata)
        metadata = metadata if isinstance(metadata, dict) else {}
        return {
            "agent_id": agent_id,
            "registered": True,
            "capabilities_hash": agent.get("capabilities_hash"),
            "capabilities_stale": bool(agent.get("capabilities_stale")),
            "readiness": metadata.get("readiness", "unknown"),
            "reason": metadata.get("readiness_reason", ""),
            "last_heartbeat": agent.get("last_heartbeat"),
        }

    @mcp.tool()
    def coord_create_task(
        creator_id: str,
        title: str,
        description: str,
        target_agent_id: str,
        capabilities_hash: str,
        allowed_tools: list[str],
        allowed_skills: list[str],
        allowed_paths: list[str],
        forbidden_paths: list[str],
        write_mode: str,
        acceptance_criteria: list[str] | dict[str, Any],
        max_turns: int,
        deadline: str,
        kind: str = "execution",
        parent_task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return get_store().create_task(
            creator_id=creator_id,
            title=title,
            description=description,
            target_agent_id=target_agent_id,
            capabilities_hash_value=capabilities_hash,
            allowed_tools=allowed_tools,
            allowed_skills=allowed_skills,
            allowed_paths=allowed_paths,
            forbidden_paths=forbidden_paths,
            write_mode=write_mode,
            acceptance_criteria=acceptance_criteria,
            max_turns=max_turns,
            deadline=deadline,
            kind=kind,
            parent_task_id=parent_task_id,
            metadata=metadata,
        )

    @mcp.tool()
    def coord_poll_messages(
        recipient_id: str,
        task_id: str | None = None,
        include_acked: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        messages = get_store().poll_messages(recipient_id=recipient_id, task_id=task_id, include_acked=include_acked, limit=limit)
        return {"messages": messages, "count": len(messages)}

    @mcp.tool()
    def coord_ack_message(message_id: str, agent_id: str) -> dict[str, Any]:
        return get_store().ack_message(message_id=message_id, agent_id=agent_id)

    @mcp.tool()
    def coord_close_task(
        task_id: str,
        verifier_id: str,
        accepted: bool,
        evidence: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return get_store().close_task(task_id=task_id, verifier_id=verifier_id, accepted=accepted, evidence=evidence, reason=reason)

    @mcp.tool()
    def coord_get_task(task_id: str) -> dict[str, Any]:
        return get_store().get_task(task_id)

    return mcp


mcp = build_server()


def main() -> None:
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
