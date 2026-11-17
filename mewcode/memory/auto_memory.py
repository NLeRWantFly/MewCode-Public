from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mewcode.client import scheduled_client_stream
from mewcode.conversation import ConversationManager, Message
from mewcode.memory.store import SQLiteMemoryStore, StoredMemory, VALID_MEMORY_TYPES
from mewcode.runtime_paths import user_data_root

USER_MEMORIES_RELPATH = ".mewcode/memories.md"
PROJECT_MEMORIES_RELPATH = ".mewcode/memories.md"
USER_DATABASE_RELPATH = ".mewcode/memory.db"
PROJECT_DATABASE_RELPATH = ".mewcode/memory.db"

MEMORY_EXTRACTION_PROMPT = """\
你是一个记忆提取助手。分析下面的对话，提取值得长期记忆的信息，更新长期记忆。

分类规则：
- **用户偏好**：用户的编码习惯和风格要求（如缩进、命名规范、语言偏好）
- **纠正反馈**：用户明确指出的错误和正确做法
- **项目知识**：当前项目的具体技术信息（技术栈、目录结构、部署方式）
- **参考资料**：外部链接和文档地址

规则：
1. 已有相同含义的条目不要重复添加
2. 没有值得记忆的内容，该分类下留空（不要写任何条目，不要写占位符）
3. 每条记忆用一行 `- ` 开头，必须是具体内容，不要用 `...` 占位
4. 输出完整的长期记忆内容，包含所有四个分类标题
5. 不要保存密码、访问令牌、API 密钥、私钥或其他认证凭据

输出格式（严格遵守，没有内容的分类下不写任何条目）：
### 用户偏好
- 用户偏好简洁代码风格

### 纠正反馈

### 项目知识
- 项目使用 PostgreSQL 15

### 参考资料

不要输出任何其他内容，不要调用任何工具。"""

_SECTION_CONFIG = {
    "用户偏好": ("user", "auto-user-preferences", "自动提取的用户偏好"),
    "纠正反馈": ("feedback", "auto-user-feedback", "自动提取的纠正反馈"),
    "项目知识": ("project", "auto-project-knowledge", "自动提取的项目知识"),
    "参考资料": ("reference", "auto-project-references", "自动提取的参考资料"),
}
_TYPE_HEADERS = {
    "user": "用户偏好",
    "feedback": "纠正反馈",
    "project": "项目知识",
    "reference": "参考资料",
}
_USER_TYPES = {"user", "feedback"}
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|password|secret|private[_-]?key)"
    r"\s*[:=]\s*\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


class MemoryManager:
    def __init__(self, project_root: str) -> None:
        self._project_root = Path(project_root).resolve()
        self._user_data_root = user_data_root()
        self._user_path = self._user_data_root / "memories.md"
        self._project_path = self._project_root / PROJECT_MEMORIES_RELPATH
        self._user_store = SQLiteMemoryStore(
            self._user_data_root / "memory.db", "user"
        )
        self._project_store = SQLiteMemoryStore(
            self._project_root / PROJECT_DATABASE_RELPATH, "project"
        )
        self._last_extraction_msg_count = 0
        self._migrate_legacy_files()
        self._migrate_legacy_directories()

    @property
    def user_path(self) -> Path:
        """Legacy Markdown export path."""
        return self._user_path

    @property
    def project_path(self) -> Path:
        """Legacy Markdown export path."""
        return self._project_path

    @property
    def user_database_path(self) -> Path:
        return self._user_store.path

    @property
    def project_database_path(self) -> Path:
        return self._project_store.path

    @property
    def user_mem_dir(self) -> Path:
        """Legacy directory retained for importing hand-written memories."""
        return self._user_data_root / "memory"

    @property
    def project_mem_dir(self) -> Path:
        """Legacy directory retained for importing hand-written memories."""
        return self._project_path.parent / "memory"

    def _store_for_type(self, memory_type: str) -> SQLiteMemoryStore:
        if memory_type not in VALID_MEMORY_TYPES:
            raise ValueError(f"Invalid memory type: {memory_type}")
        return self._user_store if memory_type in _USER_TYPES else self._project_store

    def list_memories(self, limit: int = 200) -> list[tuple[str, StoredMemory]]:
        rows = [
            *(("user", memory) for memory in self._user_store.list(limit)),
            *(("project", memory) for memory in self._project_store.list(limit)),
        ]
        rows.sort(key=lambda item: item[1].updated_at, reverse=True)
        return rows[:limit]

    def load(self) -> str:
        sections: list[str] = []
        user_text = self._render_store(self._user_store)
        project_text = self._render_store(self._project_store)
        if user_text:
            sections.append(user_text)
        if project_text:
            sections.append(project_text)
        return "\n\n".join(sections)

    def load_always_on(self) -> str:
        """Return user preferences and feedback that apply to every request."""
        return self._render_store(self._user_store)

    async def extract(
        self,
        client: Any,
        conversation: ConversationManager,
        protocol: str,
    ) -> None:
        from mewcode.tools.base import StreamEnd, TextDelta

        current_memories = self.load()
        start_index = self._last_extraction_msg_count
        recent = conversation.history[start_index:]
        if not recent:
            return

        conv_lines: list[str] = []
        for index, msg in enumerate(recent, start=start_index):
            if index == 0 and conversation.env_injected:
                continue
            if "<system-reminder>" in msg.content:
                continue
            if msg.role == "user" and msg.content:
                conv_lines.append(f"用户: {msg.content}")
            elif msg.role == "assistant" and msg.content:
                conv_lines.append(f"助手: {msg.content}")
        if not conv_lines:
            return

        prompt = (
            f"{MEMORY_EXTRACTION_PROMPT}\n\n"
            f"## 当前长期记忆\n"
            f"{current_memories if current_memories else '(空)'}\n\n"
            f"## 最近对话\n"
            f"{chr(10).join(conv_lines)}\n\n"
            f"请输出更新后的完整长期记忆内容。"
        )
        extract_conv = ConversationManager()
        extract_conv.history = [Message(role="user", content=prompt)]

        collected = ""
        try:
            async for event in scheduled_client_stream(
                client, extract_conv, system="你是一个记忆提取助手。"
            ):
                if isinstance(event, TextDelta):
                    collected += event.text
                elif isinstance(event, StreamEnd):
                    pass
        except Exception:
            return

        collected = collected.strip()
        if not collected or not self._write_memories(collected):
            return
        self._last_extraction_msg_count = len(conversation.history)

    def _write_memories(self, content: str) -> bool:
        sections = self._parse_sections(content)
        if not any(sections.values()):
            return False

        for header, (memory_type, name, description) in _SECTION_CONFIG.items():
            store = self._store_for_type(memory_type)
            lines = sections.get(header, [])
            protected_content = {
                self._normalize_memory_line(line)
                for memory in store.list()
                if memory.name != name
                for line in memory.content.splitlines()
            }
            lines = [
                line for line in lines
                if self._normalize_memory_line(line) not in protected_content
            ]
            if lines:
                store.upsert(
                    name=name,
                    content="\n".join(lines),
                    description=description,
                    memory_type=memory_type,
                    source="auto",
                )
            else:
                store.delete(name)

        self._export_legacy_markdown()
        return True

    @staticmethod
    def _parse_sections(content: str) -> dict[str, list[str]]:
        sections = {header: [] for header in _SECTION_CONFIG}
        current_header = ""
        for line in content.splitlines():
            if line.startswith("### "):
                current_header = line[4:].strip()
                continue
            if current_header not in sections:
                continue
            if (
                line.strip().startswith("- ")
                and not MemoryManager._is_placeholder(line)
                and not _SENSITIVE_VALUE_RE.search(line)
            ):
                sections[current_header].append(line.strip())
        return sections

    @staticmethod
    def _is_placeholder(line: str) -> bool:
        stripped = line.strip().lstrip("- ").strip()
        return stripped in {"", "...", "…", "无", "暂无", "N/A"}

    @staticmethod
    def _normalize_memory_line(line: str) -> str:
        return line.strip().removeprefix("- ").strip().casefold()

    def add_memory(
        self,
        name: str,
        content: str,
        description: str = "",
        memory_type: str = "reference",
    ) -> StoredMemory:
        if self._find_memory(name) is not None:
            raise ValueError(f"Memory already exists: {name}")
        memory = self._store_for_type(memory_type).add(
            name, content, description, memory_type, source="manual"
        )
        self._export_legacy_markdown()
        return memory

    def update_memory(
        self,
        name: str,
        content: str,
        description: str | None = None,
        memory_type: str | None = None,
    ) -> StoredMemory:
        found = self._find_memory(name)
        if found is None:
            raise ValueError(f"Memory not found: {name}")
        old_store, existing = found
        target_type = memory_type or existing.type
        target_store = self._store_for_type(target_type)
        if target_store is old_store:
            memory = old_store.update(name, content, description, target_type)
        else:
            memory = target_store.add(
                name,
                content,
                existing.description if description is None else description,
                target_type,
                source=existing.source,
            )
            old_store.delete(name)
        self._export_legacy_markdown()
        return memory

    def delete_memory(self, name: str) -> bool:
        deleted = self._project_store.delete(name)
        deleted = self._user_store.delete(name) or deleted
        if deleted:
            self._export_legacy_markdown()
        return deleted

    def search_memory(self, query: str, limit: int = 20) -> list[str]:
        results: list[tuple[str, StoredMemory]] = []
        results.extend(("user", row) for row in self._user_store.search(query, limit))
        results.extend(("project", row) for row in self._project_store.search(query, limit))
        results.sort(key=lambda item: item[1].updated_at, reverse=True)
        return [
            f"[{scope}/{row.type}] {row.name}: {row.content}"
            for scope, row in results[:limit]
        ]

    def search_records(
        self, query: str, limit: int = 100
    ) -> list[tuple[str, StoredMemory]]:
        results: list[tuple[str, StoredMemory]] = []
        # User preferences and feedback are always injected. Recall only needs
        # to select project facts and references for the current query.
        matched = self._project_store.search(query, limit)
        seen = {row.id for row in matched}
        matched.extend(
            row for row in self._project_store.list(limit) if row.id not in seen
        )
        results.extend(("project", row) for row in matched[:limit])
        results.sort(key=lambda item: item[1].updated_at, reverse=True)
        return results[:limit]

    def clear(self) -> None:
        self._user_store.clear()
        self._project_store.clear()
        self._export_legacy_markdown()

    def get_display_text(self) -> str:
        parts: list[str] = []
        user_text = self._render_store(self._user_store)
        project_text = self._render_store(self._project_store)
        if user_text:
            parts.append(f"[用户级 SQLite] {self.user_database_path}\n{user_text}")
        if project_text:
            parts.append(f"[项目级 SQLite] {self.project_database_path}\n{project_text}")
        return "\n\n".join(parts) if parts else "当前没有任何自动记忆。"

    def _find_memory(
        self, name: str
    ) -> tuple[SQLiteMemoryStore, StoredMemory] | None:
        for store in (self._project_store, self._user_store):
            memory = store.get(name)
            if memory is not None:
                return store, memory
        return None

    @staticmethod
    def _render_store(store: SQLiteMemoryStore) -> str:
        grouped = {memory_type: [] for memory_type in VALID_MEMORY_TYPES}
        for memory in reversed(store.list()):
            lines = memory.content.splitlines()
            grouped[memory.type].extend(
                line if line.strip().startswith("- ") else f"- {line}"
                for line in lines
                if line.strip()
            )
        sections: list[str] = []
        allowed_types = _USER_TYPES if store.scope == "user" else {"project", "reference"}
        for memory_type in _TYPE_HEADERS:
            if memory_type in allowed_types and grouped[memory_type]:
                sections.append(
                    f"### {_TYPE_HEADERS[memory_type]}\n" + "\n".join(grouped[memory_type])
                )
        return "\n\n".join(sections)

    def _migrate_legacy_files(self) -> None:
        for store, path in (
            (self._user_store, self._user_path),
            (self._project_store, self._project_path),
        ):
            if store.get_metadata("legacy_markdown_migrated") == "1":
                continue
            if path.exists():
                try:
                    sections = self._parse_sections(path.read_text(encoding="utf-8"))
                except OSError:
                    sections = {}
                allowed = _USER_TYPES if store.scope == "user" else {"project", "reference"}
                for header, lines in sections.items():
                    memory_type, name, description = _SECTION_CONFIG[header]
                    if memory_type in allowed and lines and store.get(name) is None:
                        store.upsert(
                            name,
                            "\n".join(lines),
                            description,
                            memory_type,
                            source="legacy",
                        )
            store.set_metadata("legacy_markdown_migrated", "1")

    def _migrate_legacy_directories(self) -> None:
        from mewcode.memory.recall import ENTRYPOINT_NAME, FRONTMATTER_RE, parse_frontmatter

        for store, directory in (
            (self._user_store, self.user_mem_dir),
            (self._project_store, self.project_mem_dir),
        ):
            if store.get_metadata("legacy_directory_migrated") == "1":
                continue
            if directory.is_dir():
                for path in directory.rglob("*.md"):
                    if not path.is_file() or path.name == ENTRYPOINT_NAME:
                        continue
                    try:
                        raw = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    frontmatter = parse_frontmatter(raw)
                    memory_type = frontmatter["type"] or (
                        "user" if store.scope == "user" else "project"
                    )
                    allowed = _USER_TYPES if store.scope == "user" else {"project", "reference"}
                    if memory_type not in allowed:
                        continue
                    name = frontmatter["name"] or path.stem
                    body = FRONTMATTER_RE.sub("", raw, count=1).strip()
                    if not body or store.get(name) is not None:
                        continue
                    try:
                        store.add(
                            name=name,
                            content=body,
                            description=frontmatter["description"],
                            memory_type=memory_type,
                            source="legacy-file",
                        )
                    except ValueError:
                        continue
            store.set_metadata("legacy_directory_migrated", "1")

    def _export_legacy_markdown(self) -> None:
        for store, path in (
            (self._user_store, self._user_path),
            (self._project_store, self._project_path),
        ):
            text = self._render_store(store)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text + ("\n" if text else ""), encoding="utf-8")
            except OSError:
                # Markdown is a compatibility view; SQLite remains authoritative.
                continue
