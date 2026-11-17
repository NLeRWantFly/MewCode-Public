"""Local skill discovery without installs, downloads, or tool activation."""
import json
import re
from pydantic import BaseModel, Field
from mewcode.tools.base import Tool, ToolResult


class FindSkillsParams(BaseModel):
    query: str = Field(default="", description="Task keywords; empty lists available skills")
    limit: int = Field(default=8, ge=1, le=20)


class FindSkills(Tool):
    name = "FindSkills"
    description = (
        'Discover an installed skill for the current task before loading its instructions. '
        'Pass short task keywords in query, or {"query":""} to list the catalog (bounded by limit). '
        'Returns skills[{name,description}], total_matches and installed_only=true. '
        'Use an exact returned name with LoadSkill; discovery does not activate a skill or prove its steps were followed. '
        'No matches: try a shorter keyword or empty query; do not assume a skill exists or silently install one. '
        'Reads project, user, built-in and plugin catalogs without downloading, executing skill code, or adding permissions.')
    category = "read"
    is_system_tool = True
    is_concurrency_safe = True
    params_model = FindSkillsParams

    def __init__(self, loader):
        self.loader = loader

    async def execute(self, params):
        words = re.findall(r"[\w-]+", params.query.casefold())
        matches = []
        for name, description in self.loader.get_catalog():
            text = f"{name} {description}".casefold()
            score = sum(3 if word in name.casefold() else 1 for word in words if word in text)
            if not words or score:
                matches.append((score, name, description))
        matches.sort(key=lambda item: (-item[0], item[1]))
        return ToolResult(output=json.dumps({
            "skills": [{"name": name, "description": description} for _, name, description in matches[:params.limit]],
            "total_matches": len(matches), "installed_only": True,
        }, ensure_ascii=False))
