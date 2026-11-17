from __future__ import annotations

from pathlib import Path

from mewcode.mcp.coord_server import MCP_INSTRUCTIONS, build_server
from mewcode.mcp.coord_store import CoordinationStore
from mewcode.mcp.coord_protocol import APPROVAL_MODEL_ID


def test_missing_config_keeps_coordination_available_without_model_routes(tmp_path, monkeypatch):
    import mewcode.config as config
    import mewcode.mcp.coord_server as module

    def missing():
        raise config.ConfigError("No config file found")

    monkeypatch.setattr(config, "load_config", missing)
    monkeypatch.setattr(module, "project_root", lambda: tmp_path)
    server = build_server()
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert "coord_protocol" in names
    assert "coord_team_v2_execute" not in names
    assert not (tmp_path / ".mewcode").exists()


def test_fastmcp_exposes_coordination_tools_only(tmp_path: Path) -> None:
    store = CoordinationStore(tmp_path / "state.sqlite3", tmp_path / "capabilities")
    try:
        server = build_server(store)
        names = {tool.name for tool in server._tool_manager.list_tools()}
        assert names == {
            "coord_protocol",
            "coord_register_agent",
            "coord_sync_capabilities",
            "coord_get_capabilities",
            "coord_get_executor_status",
            "coord_create_task",
            "coord_poll_messages",
            "coord_ack_message",
            "coord_close_task",
            "coord_get_task",
        }
        assert "coord_claim_task" not in names
        assert "no shell/file/browser/model execution" in MCP_INSTRUCTIONS
        assert "run_terminal_cmd" in MCP_INSTRUCTIONS
        assert "at least two Grok turns" in MCP_INSTRUCTIONS
        protocol = server._tool_manager.get_tool("coord_protocol").fn()
        assert protocol["approval_contract"]["model_id"] == APPROVAL_MODEL_ID
        assert protocol["external_model_contract"]["model_id"] == "grok-4.6"
        assert protocol["external_model_contract"]["read_only_defaults"]["reasoning_effort"] == "high"
        assert protocol["external_model_contract"]["invocation_boundary"].startswith("explicit external runner")
        assert protocol["external_model_contract"]["terminal_task_constraints"] == {
            "tool": "run_terminal_cmd",
            "min_max_turns": 2,
            "max_max_turns": 3,
        }
        assert protocol["trigger"]["word"] == "协同"
        store.register_agent(
            agent_id="grok-executor-01",
            model_id="grok-4.6",
            role="executor",
            session_id="mcp-test-executor",
            metadata={
                "runner": "mewcode-coord-worker",
                "readiness": "auth_required",
                "readiness_reason": "Grok authentication is required",
            },
        )
        status = server._tool_manager.get_tool("coord_get_executor_status").fn()
        assert status["readiness"] == "auth_required"
        assert status["reason"] == "Grok authentication is required"
        store.register_agent(
            agent_id="codex-verifier-01",
            model_id=APPROVAL_MODEL_ID,
            role="verifier",
            session_id="mcp-test-verifier",
        )
        assert server._tool_manager.get_tool("coord_poll_messages").fn("codex-verifier-01", None, False, 50) == {"messages": [], "count": 0}
    finally:
        store.close()
