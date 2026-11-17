from __future__ import annotations

from mewcode.skills.loader import SkillLoader
from mewcode.skills.parser import render_skill_prompt


def test_web_access_is_packaged_as_a_builtin_skill() -> None:
    loader = SkillLoader("/nonexistent")
    skills = loader.load_all()

    assert "web-access" in skills
    skill = skills["web-access"]
    assert skill.is_directory is True
    assert skill.source_path is not None
    assert (skill.source_path.parent / "scripts" / "check-deps.mjs").is_file()
    assert (skill.source_path.parent / "references" / "cdp-api.md").is_file()


def test_web_access_skill_directory_placeholder_is_rendered() -> None:
    loader = SkillLoader("/nonexistent")
    skill = loader.load_all()["web-access"]

    rendered = render_skill_prompt(skill)

    assert "${CLAUDE_SKILL_DIR}" not in rendered
    assert "$CLAUDE_SKILL_DIR" not in rendered
    assert str(skill.source_path.parent.resolve()) in rendered
    assert "<skill-environment>" in rendered
