from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.conversation import ConversationManager
from mewcode.harness.tools import ManageMemoryParams, ManageMemoryTool
from mewcode.memory.auto_memory import MemoryManager
from mewcode.memory.recall import find_relevant_memories, render_reminder
from mewcode.memory.store import SQLiteMemoryStore
from mewcode.tools.base import StreamEnd, TextDelta


@pytest.fixture
def isolated_memory_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(home / ".mewcode"))
    return home


@pytest.fixture
def memory_manager(tmp_path: Path, isolated_memory_home: Path) -> MemoryManager:
    project = tmp_path / "project"
    project.mkdir()
    return MemoryManager(str(project))


class TestSQLiteMemoryStore:
    def test_crud_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "memory.db"
        store = SQLiteMemoryStore(path, "project")
        created = store.add("database", "Uses SQLite", "Database choice", "project")

        reopened = SQLiteMemoryStore(path, "project")
        loaded = reopened.get(created.id)
        assert loaded is not None
        assert loaded.name == "database"
        assert loaded.content == "Uses SQLite"

        reopened.update("database", "Uses SQLite with WAL")
        assert reopened.search("WAL")[0].content == "Uses SQLite with WAL"
        assert reopened.delete("database") is True
        assert reopened.get("database") is None

    def test_duplicate_name_is_rejected(self, tmp_path: Path) -> None:
        store = SQLiteMemoryStore(tmp_path / "memory.db", "user")
        store.add("style", "Use concise code", memory_type="user")
        with pytest.raises(ValueError, match="already exists"):
            store.add("style", "Use verbose code", memory_type="user")


class TestSQLiteMemoryManager:
    def test_scope_routing_and_type_move(self, memory_manager: MemoryManager) -> None:
        memory_manager.add_memory("style", "Use spaces", memory_type="user")
        memory_manager.add_memory("database", "Uses SQLite", memory_type="project")

        assert memory_manager.user_database_path.exists()
        assert memory_manager.project_database_path.exists()
        display = memory_manager.get_display_text()
        assert "Use spaces" in display
        assert "Uses SQLite" in display
        assert "Use spaces" in memory_manager.load_always_on()
        assert "Uses SQLite" not in memory_manager.load_always_on()

        memory_manager.update_memory(
            "database", "Prefer SQLite", memory_type="feedback"
        )
        rows = memory_manager.list_memories()
        moved = next((scope, row) for scope, row in rows if row.name == "database")
        assert moved[0] == "user"
        assert moved[1].type == "feedback"

    def test_automatic_extraction_preserves_manual_memory(
        self, memory_manager: MemoryManager
    ) -> None:
        memory_manager.add_memory("manual-rule", "Never commit secrets", memory_type="user")
        assert memory_manager._write_memories(
            "### 用户偏好\n- Use short functions\n\n"
            "### 纠正反馈\n\n"
            "### 项目知识\n- Uses SQLite\n\n"
            "### 参考资料\n"
        )
        loaded = memory_manager.load()
        assert "Never commit secrets" in loaded
        assert "Use short functions" in loaded
        assert "Uses SQLite" in loaded

    def test_sensitive_values_are_not_stored(self, memory_manager: MemoryManager) -> None:
        memory_manager._write_memories(
            "### 用户偏好\n- Use concise answers\n"
            "- api_key = sk-sensitive-value\n\n"
            "### 纠正反馈\n\n### 项目知识\n\n### 参考资料\n"
        )
        loaded = memory_manager.load()
        assert "Use concise answers" in loaded
        assert "sk-sensitive-value" not in loaded

    def test_imports_flat_and_directory_legacy_memories(
        self, tmp_path: Path, isolated_memory_home: Path
    ) -> None:
        project = tmp_path / "project"
        legacy_flat = project / ".mewcode" / "memories.md"
        legacy_flat.parent.mkdir(parents=True)
        legacy_flat.write_text("### 项目知识\n- Legacy project fact\n", encoding="utf-8")
        legacy_dir = project / ".mewcode" / "memory"
        legacy_dir.mkdir()
        (legacy_dir / "deployment.md").write_text(
            "---\nname: deployment\ndescription: deployment notes\n"
            "type: reference\n---\nDeploy with Docker",
            encoding="utf-8",
        )
        manager = MemoryManager(str(project))
        assert "Legacy project fact" in manager.load()
        assert "Deploy with Docker" in manager.load()


@pytest.mark.asyncio
async def test_extraction_ignores_injected_context(memory_manager: MemoryManager) -> None:
    class ExtractClient:
        prompt = ""

        async def stream(self, conversation, system=""):
            self.prompt = conversation.history[0].content
            yield TextDelta("### 用户偏好\n- Use tests\n\n### 纠正反馈\n")
            yield TextDelta("\n### 项目知识\n\n### 参考资料\n")
            yield StreamEnd("end_turn")

    conversation = ConversationManager()
    conversation.inject_environment("INTERNAL ENVIRONMENT")
    conversation.add_system_reminder("INTERNAL REMINDER")
    conversation.add_user_message("Please use tests")
    client = ExtractClient()

    await memory_manager.extract(client, conversation, "anthropic")
    assert "Please use tests" in client.prompt
    assert "INTERNAL ENVIRONMENT" not in client.prompt
    assert "INTERNAL REMINDER" not in client.prompt
    assert "Use tests" in memory_manager.load()


@pytest.mark.asyncio
async def test_database_recall_uses_stable_ids(memory_manager: MemoryManager) -> None:
    memory = memory_manager.add_memory(
        "database", "The project uses SQLite WAL mode", "database architecture", "project"
    )
    stable_id = f"project:{memory.id}"

    async def selector(system_prompt: str, user_message: str) -> str:
        assert stable_id in user_message
        assert str(memory_manager.project_database_path) not in user_message
        return json.dumps({"selected_memories": [stable_id]})

    selected = await find_relevant_memories(
        query="Which database is used?",
        user_mem_dir=None,
        project_mem_dir=None,
        recent_tools=None,
        already_surfaced=None,
        selector=selector,
        memory_manager=memory_manager,
    )
    assert len(selected) == 1
    assert selected[0].content == "The project uses SQLite WAL mode"
    assert "SQLite WAL mode" in render_reminder(selected)

    async def should_not_run(system_prompt: str, user_message: str) -> str:
        raise AssertionError("already surfaced memories should be filtered first")

    repeated = await find_relevant_memories(
        query="Which database is used?",
        user_mem_dir=None,
        project_mem_dir=None,
        recent_tools=None,
        already_surfaced={stable_id},
        selector=should_not_run,
        memory_manager=memory_manager,
    )
    assert repeated == []


@pytest.mark.asyncio
async def test_manage_memory_tool_supports_full_crud(
    memory_manager: MemoryManager,
) -> None:
    tool = ManageMemoryTool(memory_manager)
    added = await tool.execute(
        ManageMemoryParams(
            action="add", name="style", content="Use spaces", memory_type="user"
        )
    )
    assert added.is_error is False

    updated = await tool.execute(
        ManageMemoryParams(action="update", name="style", content="Use four spaces")
    )
    assert updated.is_error is False
    assert any("four spaces" in row for row in memory_manager.search_memory("spaces"))

    deleted = await tool.execute(ManageMemoryParams(action="delete", name="style"))
    assert deleted.is_error is False
    assert memory_manager.search_memory("four spaces") == []
