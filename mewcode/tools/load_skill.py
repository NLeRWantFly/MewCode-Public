from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from mewcode.skills.parser import render_skill_prompt
from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.agent import Agent
    from mewcode.skills.directory import register_skill_tools
    from mewcode.skills.loader import SkillLoader


class LoadSkillParams(BaseModel):
    name: str = Field(description="Exact installed skill name from the visible catalog or FindSkills; not a file path, keyword, or tool name.")


class LoadSkill(Tool):
    name = "LoadSkill"
    description = (
        'Activate an installed skill when its task description matches the current work. '
        'Use an exact catalog name, e.g. {"name":"the-name-returned-by-FindSkills"}; discover with FindSkills first if unknown. '
        "Success pins the skill SOP into this agent's environment context; follow its relevant steps on subsequent calls. "
        "The result reports activation and any tools registered. In orchestration nodes, activation is instruction-only: "
        "allowed tools, explicit denials and permission scope stay unchanged, and skill code is not registered. "
        "Outside instruction-only mode, trusted directory skills may register tools; installed instruction-only skills cannot. "
        "An unknown name returns the available catalog; choose from it instead of inventing a name. "
        "An initialization error requires host setup. Loading is not installation, task execution, or evidence of completion."
    )
    params_model = LoadSkillParams
    category = "read"
    is_concurrency_safe = False
    is_system_tool = True


    def __init__(self, *, instruction_only: bool = False) -> None:
        self._loader: SkillLoader | None = None
        self._agent: Agent | None = None
        self.instruction_only = instruction_only


    def set_loader(self, loader: SkillLoader) -> None:
        self._loader = loader

    def set_agent(self, agent: Agent) -> None:
        self._agent = agent


    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, LoadSkillParams)

        if self._loader is None or self._agent is None:
            return ToolResult(
                output="Error: LoadSkill not properly initialized",
                is_error=True,
            )

        skill = self._loader.get(params.name)
        if skill is None:
            available = ", ".join(n for n, _ in self._loader.get_catalog())
            return ToolResult(
                output=f"Error: unknown skill '{params.name}'. Available skills: {available}",
                is_error=True,
            )
        from mewcode.skills.runtime import format_skill_catalog
        self._agent.set_skill_catalog(format_skill_catalog(self._loader))

        self._agent.activate_skill(skill.name, render_skill_prompt(skill))

        tool_count = 0
        installed_instruction_only = bool(skill.source_path and
            (skill.source_path.parent / ".mewcode-install.json").is_file())
        if not self.instruction_only and not installed_instruction_only and skill.is_directory and skill.source_path is not None:
            from mewcode.skills.directory import register_skill_tools
            skill_dir = skill.source_path.parent
            tool_count = register_skill_tools(skill_dir, self._agent.registry)

        parts = [f"Skill '{skill.name}' activated. SOP pinned to environment context."]
        if self.instruction_only or installed_instruction_only:
            parts.append("Instruction-only activation: this node's allowed tools and permission scope are unchanged.")
        if tool_count > 0:
            parts.append(f"{tool_count} specialized tool(s) registered.")
        return ToolResult(output=" ".join(parts))
