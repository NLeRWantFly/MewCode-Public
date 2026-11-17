"""Explicit permission-checked installation, separate from skill discovery."""
import json

from pydantic import BaseModel, Field

from mewcode.skills import installer
from mewcode.tools.base import Tool, ToolResult, ToolRiskProfile


class InstallSkillParams(BaseModel):
    repo: str = Field(description="Public GitHub owner/repo explicitly selected by the user")
    path: str = Field(description="Exact repository directory containing SKILL.md, e.g. skills/typesafe-ai")
    ref: str = Field(default="main", description="Git revision, tag, or branch; prefer a pinned commit")


class InstallSkill(Tool):
    name = "InstallSkill"
    description = ("Install a user-requested public GitHub skill into this project's .mewcode/skills. "
                   "No npx, PowerShell, dependency installation or downloaded code execution. "
                   "Never overwrites an existing skill. Refreshes the local catalog; use LoadSkill afterward.")
    category = "harness"
    is_system_tool = True
    params_model = InstallSkillParams

    def __init__(self, work_dir, loader, agent):
        self.work_dir, self.loader, self.agent = work_dir, loader, agent

    def permission_risk(self, arguments):
        return ToolRiskProfile(side_effect="workspace", network=True, target_type="skill_install")

    async def execute(self, params):
        try:
            result = await installer.install(self.work_dir, **params.model_dump())
        except Exception as exc:
            # Do not emit server bodies, credentials or archive content.
            from mewcode.skills.parser import SkillParseError
            message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            if isinstance(exc, SkillParseError):
                message = "Invalid SKILL.md frontmatter"
            return ToolResult(output=f"Skill installation stopped: {message}", is_error=True)
        self.loader.reload()
        from mewcode.skills.runtime import format_skill_catalog
        self.agent.set_skill_catalog(format_skill_catalog(self.loader))
        return ToolResult(output=json.dumps(result, ensure_ascii=False))
