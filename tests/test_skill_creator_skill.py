from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from mewcode.skills.loader import SkillLoader
from mewcode.skills.parser import render_skill_prompt
from mewcode.tools import ToolRegistry


def test_skill_creator_is_packaged_as_a_builtin_skill() -> None:
    loader = SkillLoader("/nonexistent")
    skills = loader.load_all()

    assert "skill-creator" in skills
    skill = skills["skill-creator"]
    assert skill.is_directory is True
    assert skill.source_path is not None
    skill_dir = skill.source_path.parent
    assert (skill_dir / "scripts" / "quick_validate.py").is_file()
    assert (skill_dir / "scripts" / "run_eval.py").is_file()
    assert (skill_dir / "eval-viewer" / "generate_review.py").is_file()
    assert (skill_dir / "LICENSE.txt").is_file()


def test_skill_creator_directory_placeholder_is_rendered() -> None:
    loader = SkillLoader("/nonexistent")
    skill = loader.load_all()["skill-creator"]

    rendered = render_skill_prompt(skill, "make a pdf skill")

    assert "${CLAUDE_SKILL_DIR}" not in rendered
    assert "$CLAUDE_SKILL_DIR" not in rendered
    assert "${MEWCODE_SKILL_DIR}" not in rendered
    assert str(skill.source_path.parent.resolve()) in rendered
    assert "<skill-environment>" in rendered
    assert "make a pdf skill" in rendered or "$ARGUMENTS" not in skill.prompt_body


def test_skill_creator_prompt_uses_mewcode_paths() -> None:
    loader = SkillLoader("/nonexistent")
    body = loader.load_all()["skill-creator"].prompt_body
    assert ".mewcode/skills/" in body
    assert "LoadSkill" in body
    assert "claude -p" not in body


def test_quick_validate_accepts_mewcode_frontmatter(tmp_path: Path) -> None:
    from mewcode.skills.skill_validate import validate_skill

    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: Demo skill for validation\n"
        "allowedTools:\n"
        "  - ReadFile\n"
        "  - Bash\n"
        "mode: inline\n"
        "context: full\n"
        "---\n\n"
        "# Demo\n",
        encoding="utf-8",
    )
    valid, message = validate_skill(skill_dir)
    assert valid is True, message


def test_summarize_eval_marks_trigger_threshold() -> None:
    from mewcode.skills.skill_eval import summarize_eval

    eval_set = [
        {"query": "create a skill", "should_trigger": True},
        {"query": "what's the weather", "should_trigger": False},
    ]
    trigger_results = {
        "create a skill": [True, True, False],
        "what's the weather": [False, False, False],
    }
    output = summarize_eval(
        eval_set,
        trigger_results,
        skill_name="skill-creator",
        description="Create skills",
        trigger_threshold=0.5,
    )
    assert output["summary"]["passed"] == 2
    assert output["summary"]["total"] == 2
    by_query = {row["query"]: row for row in output["results"]}
    assert by_query["create a skill"]["pass"] is True
    assert by_query["what's the weather"]["pass"] is True


def test_run_eval_uses_injected_detect_fn() -> None:
    from mewcode.skills.skill_eval import run_eval

    eval_set = [{"query": "make a skill", "should_trigger": True}]
    output = run_eval(
        eval_set,
        skill_name="skill-creator",
        description="Create skills",
        detect_fn=lambda _query: True,
        runs_per_query=2,
    )
    assert output["summary"]["passed"] == 1
    assert output["results"][0]["triggers"] == 2
    assert output["results"][0]["runs"] == 2


def test_attach_skills_registers_loadskill_and_catalog() -> None:
    from mewcode.skills.runtime import attach_skills

    agent = MagicMock()
    registry = ToolRegistry()
    loader = attach_skills("/nonexistent", agent, registry)

    assert registry.get("LoadSkill") is not None
    names = [n for n, _ in loader.get_catalog()]
    assert "commit" in names
    assert "skill-creator" in names
    agent.set_skill_catalog.assert_called_once()
    catalog_text = agent.set_skill_catalog.call_args[0][0]
    assert "skill-creator" in catalog_text
    assert "LoadSkill" in catalog_text


def test_tui_and_headless_prompt_share_runtime_skill_registration() -> None:
    import inspect

    import mewcode.__main__ as mainmod
    import mewcode.app as appmod
    from mewcode.plugins.runtime import AgentRuntimeFactory

    headless_entry = inspect.getsource(mainmod._run_prompt)
    headless_runtime = inspect.getsource(mainmod._run_prompt_with_host)
    assert "plugin_host.context.get(AGENT_FACTORY)" in headless_runtime
    assert "factory.acreate(AgentRuntimeRequest(" in headless_runtime
    assert "finally" in headless_entry
    assert "plugin_host.aclose" in headless_entry
    tui_runtime = inspect.getsource(appmod.MewCodeApp._select_provider)
    assert "self.context.get(AGENT_FACTORY).create(AgentRuntimeRequest(" in tui_runtime
    shared_runtime = inspect.getsource(AgentRuntimeFactory._apply)
    assert "if request.enable_skills:" in shared_runtime
    assert 'tool = LoadSkill()' in shared_runtime
    assert "tool.set_loader(loader)" in shared_runtime
    assert "tool.set_agent(agent)" in shared_runtime
    assert "agent.set_skill_catalog(catalog)" in shared_runtime
