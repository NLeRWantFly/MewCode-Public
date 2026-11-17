import json
from pathlib import Path

from frontend import server


def _session(monkeypatch, tmp_path: Path) -> server.WebSession:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / "web_state.json")
    monkeypatch.setattr(server, "PROJECTS_FILE", tmp_path / "projects.json")
    monkeypatch.setattr(
        server, "BACKGROUND_TASKS_FILE", tmp_path / "background_tasks.json"
    )
    monkeypatch.chdir(project)
    session = server.WebSession()
    session.new_conversation("project")
    return session


def test_plan_and_do_commands_restore_previous_permission(monkeypatch, tmp_path) -> None:
    session = _session(monkeypatch, tmp_path)
    assert session.set_permission_mode("auto") is None

    entered = session.execute_web_command("plan", "分析修改方案")
    assert entered["ok"] is True
    assert entered["prompt"] == "分析修改方案"
    assert session.permission_mode.value == "plan"

    exited = session.execute_web_command("do")
    assert exited["ok"] is True
    assert session.permission_mode.value == "auto"
    session._close_session()


def test_goal_command_is_conversation_scoped_and_persisted(monkeypatch, tmp_path) -> None:
    session = _session(monkeypatch, tmp_path)
    conversation_id = session.session.session_id

    result = session.execute_web_command("goal", "修复所有失败测试")

    assert result["ok"] is True
    assert result["prompt"] == "修复所有失败测试"
    assert session.active_goal() == "修复所有失败测试"
    assert session.status_payload()["goal_mode"] is True
    state = json.loads(server.STATE_FILE.read_text(encoding="utf-8"))
    assert state["conversation_goals"][conversation_id] == "修复所有失败测试"

    cleared = session.execute_web_command("goal", "off")
    assert cleared["ok"] is True
    assert session.active_goal() == ""
    session._close_session()


def test_orchestration_planning_preserves_selected_permission(monkeypatch, tmp_path) -> None:
    session = _session(monkeypatch, tmp_path)
    assert session.set_permission_mode("auto") is None
    assert session.set_orchestration_planning(True) is None
    assert session.orchestration_planning is True
    assert session.permission_mode.value == "auto"
    assert session.agent is None or session.agent.orchestration_planning is True
    assert session.status_payload()["orchestration_planning"] is True

    assert session.set_orchestration_planning(False) is None
    assert session.orchestration_planning is False
    assert session.permission_mode.value == "auto"
    session._close_session()


def test_unknown_web_command_returns_actionable_help(monkeypatch, tmp_path) -> None:
    session = _session(monkeypatch, tmp_path)

    result = session.execute_web_command("missing")

    assert result["ok"] is False
    assert "/help" in result["message"]
    session._close_session()
