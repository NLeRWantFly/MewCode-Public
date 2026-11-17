from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mewcode.skills.loader import SkillLoader
from mewcode.tools.load_skill import LoadSkill
from mewcode.tools import ToolRegistry


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        if (parent / ".mewcode").is_dir() or (parent / ".git").is_dir():
            return parent
    return current


def format_skill_catalog(loader: SkillLoader) -> str:
    catalog = loader.get_catalog()
    if not catalog:
        return ""
    lines = [
        "You can use the following Skills:",
        "",
    ]
    for name, desc in catalog:
        lines.append(f"- {name}: {desc}")
    lines.append("")
    lines.append(
        "If the user's request matches a Skill, call LoadSkill to activate it."
    )
    return "\n".join(lines)


def attach_skills(
    work_dir: str,
    agent: Any,
    registry: ToolRegistry,
    *,
    loader: SkillLoader | None = None,
    instruction_only: bool = False,
    enable_load: bool = True,
    enable_find: bool = True,
) -> SkillLoader:
    loader = loader or SkillLoader(work_dir)
    loader.load_all()

    if enable_load:
        # Preserve host instrumentation (e.g. RecordingLoadSkill). Expert nodes
        # exclude the parent-bound tool before reaching this attachment point.
        tool = registry.get("LoadSkill")
        if tool is None:
            tool = LoadSkill(instruction_only=instruction_only)
            registry.register(tool)
        tool.instruction_only = instruction_only
        tool.set_loader(loader)
        tool.set_agent(agent)
    if enable_find:
        from mewcode.tools.find_skills import FindSkills
        registry.register(FindSkills(loader))
    if not instruction_only and enable_load and enable_find:
        from mewcode.tools.install_skill import InstallSkill
        registry.register(InstallSkill(work_dir, loader, agent))
    else:
        registry.unregister("InstallSkill")

    catalog_text = format_skill_catalog(loader)
    if catalog_text and enable_load:
        agent.set_skill_catalog(catalog_text)
    return loader


def detect_skill_trigger(query: str, skill_name: str, work_dir: str) -> bool:
    return asyncio.run(_detect_skill_trigger_async(query, skill_name, work_dir))


async def _detect_skill_trigger_async(
    query: str, skill_name: str, work_dir: str
) -> bool:
    from mewcode.config import load_config
    from mewcode.skills.skill_eval import RecordingLoadSkill
    from mewcode.plugins import ApplicationHost
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.plugins.services import AGENT_FACTORY

    config = load_config()
    provider = config.providers[0]
    host = ApplicationHost(work_dir)
    await host.astart()
    runtime = None
    try:
        recorder = RecordingLoadSkill()
        runtime = await host.context.get(AGENT_FACTORY).acreate(AgentRuntimeRequest(
            provider=provider,
            work_dir=work_dir,
            max_iterations=8,
            extra_tools=(recorder,),
        ))
        await runtime.agent.run_to_completion(query, runtime.conversation)
        return skill_name in recorder.invoked
    finally:
        if runtime is not None:
            await runtime.aclose()
        await host.aclose()
