from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Literal

from pydantic import BaseModel

from mewcode.config import ProviderConfig


_MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
}


async def run_agent_call(
    *,
    parent_agent: Any,
    provider: ProviderConfig,
    prompt: str,
    schema: type[BaseModel] | None = None,
    model: str | None = None,
    model_hint: str | None = None,
    effort: str | None = None,
    isolation: str | None = None,
    tools: list[str] | None = None,
    worktree_manager: Any = None,
) -> tuple[Any, dict[str, int]]:
    """Run an orchestration call in a fresh Agent and return its token usage."""
    if parent_agent is None:
        raise RuntimeError("parent agent is not available")

    selected_model = model
    if not selected_model and model_hint and provider.protocol == "anthropic":
        selected_model = model_hint
    if selected_model == "inherit":
        selected_model = None
    model_id = _MODEL_ALIASES.get(selected_model or "", selected_model)

    child_config = replace(
        provider,
        name=f"orchestration-{selected_model or provider.model}",
        model=model_id or provider.model,
    )
    work_dir = parent_agent.work_dir
    task = prompt
    worktree_name: str | None = None
    worktree = None
    if isolation:
        if isolation != "worktree":
            raise ValueError(f"unsupported isolation mode: {isolation}")
        if worktree_manager is None:
            raise RuntimeError("worktree isolation is not available")
        from mewcode.worktree.integration import (
            build_worktree_notice,
            generate_worktree_name,
        )

        worktree_name = generate_worktree_name()
        worktree = await worktree_manager.create(worktree_name, "HEAD")
        work_dir = worktree.path
        task = build_worktree_notice(parent_agent.work_dir, work_dir) + "\n" + task

    if schema is not None:
        task += _schema_instruction(schema)

    registry = _build_registry(parent_agent, tools)
    checker = _clone_permission_checker(
        parent_agent, work_dir, profile="worktree" if worktree is not None else "standard",
    )
    # Reject invalid policy inheritance before opening a runtime/client.
    runtime_context = getattr(parent_agent, "runtime_context", None)
    fallback_host = None
    if runtime_context is None:
        from mewcode.plugins import ApplicationHost

        fallback_host = ApplicationHost(
            parent_agent.work_dir,
            discover_plugins=False,
        ).start()
        runtime_context = fallback_host.context
    from mewcode.plugins.services import CLIENT_FACTORY

    client = runtime_context.get(CLIENT_FACTORY).create(child_config)
    if effort:
        client.set_effort(effort)
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.plugins.services import AGENT_FACTORY

    cordis_runtime = runtime_context.get(AGENT_FACTORY).create(
        AgentRuntimeRequest(
            provider=child_config,
            work_dir=work_dir,
            client=client,
            auxiliary_client=client,
            permission_checker=checker,
            max_iterations=parent_agent.max_iterations,
            instructions_content=parent_agent.instructions_content,
            memory_manager=parent_agent.memory_manager,
            enable_memory=False,
            hook_engine=parent_agent.hook_engine,
            enable_skills=False,
            enable_tool_search=False,
            include_default_tools=False,
            inherit_parent_tools=False,
            extra_tools=tuple(registry.list_tools()),
        )
    )
    child = cordis_runtime.agent
    child.parent_id = parent_agent.agent_id
    child.trace_id = parent_agent.trace_id or parent_agent.agent_id

    try:
        result_text = await child.run_to_completion(task)
        usage = {
            "input_tokens": child.total_input_tokens,
            "output_tokens": child.total_output_tokens,
        }
    finally:
        if cordis_runtime is not None:
            await cordis_runtime.aclose()
        if fallback_host is not None:
            await fallback_host.aclose()

    if worktree is not None and worktree_name is not None:
        cleanup = await worktree_manager.auto_cleanup(
            worktree_name, worktree.head_commit
        )
        if cleanup.kept:
            result_text = (result_text or "") + (
                f"\n[Worktree preserved at {cleanup.path}, branch {cleanup.branch}]"
            )

    if schema is not None:
        return _validate_structured_result(schema, result_text), usage
    return result_text, usage


def _build_registry(parent_agent: Any, tools: list[str] | None):
    from mewcode.agents.parser import AgentDef
    from mewcode.agents.tool_filter import resolve_agent_tools
    from mewcode.tools import ToolRegistry

    if tools == []:
        return ToolRegistry()

    source = getattr(parent_agent, "_full_registry", None) or parent_agent.registry
    enabled = ToolRegistry()
    for tool in source.list_tools():
        if source.is_enabled(tool.name):
            enabled.register(tool)
            if source.is_discovered(tool.name):
                enabled.mark_discovered(tool.name)

    definition = AgentDef(
        agent_type="orchestration",
        when_to_use="Workflow and pipeline execution",
        tools=list(tools or []),
    )
    registry = resolve_agent_tools(enabled, definition, is_background=False)
    for tool in registry.list_tools():
        if enabled.is_discovered(tool.name):
            registry.mark_discovered(tool.name)
    return registry


def _clone_permission_checker(
    parent_agent: Any, work_dir: str, *,
    profile: Literal["standard", "auditor", "worktree"] = "standard",
):
    parent = parent_agent.permission_checker
    if parent is None:
        return None

    from mewcode.permissions.policy import build_permission_checker

    # A same-workspace clone must retain a parent's narrower declared write
    # set, not recreate a sandbox granting the whole workspace. Worktrees
    # deliberately receive only their own root, bounded by the parent policy.
    return build_permission_checker(
        work_dir,
        parent_policy=parent,
        profile=profile,
        writable_scope=getattr(parent, "policy_writable_roots", None)
        if profile == "standard" else None,
    )


def _schema_instruction(schema: type[BaseModel]) -> str:
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return (
        "\n\nReturn only one JSON object matching this JSON Schema. "
        "Do not wrap it in prose or Markdown:\n" + schema_json
    )


def _validate_structured_result(
    schema: type[BaseModel], text: str
) -> BaseModel:
    stripped = text.strip()
    candidates = [stripped]
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(
            r"```(?:json)?\s*([\s\S]*?)```", stripped, flags=re.IGNORECASE
        )
    )

    decoder = json.JSONDecoder()
    for start, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            continue
        return schema.model_validate(obj)

    for candidate in candidates:
        try:
            return schema.model_validate_json(candidate)
        except Exception:
            continue
    raise ValueError(f"agent returned invalid structured output for {schema.__name__}")
