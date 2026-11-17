"""Aggregate local and remote conversations into a machine → source → project tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

SOURCE_ORDER = ("mewcode", "codex", "claude-code")
SOURCE_LABELS = {
    "mewcode": "MewCode",
    "codex": "Codex",
    "claude-code": "Claude Code",
}
NORMAL_PROJECT_ID = "普通对话"
PURE_CHAT_ID = "纯对话"
MAX_IMPORT_MESSAGES = 40
MAX_IMPORT_CHARS = 24000


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source or "MewCode")


def sort_history_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep MewCode sessions first, then Codex / Claude Code, newest within each source."""
    rank = {name: index for index, name in enumerate(SOURCE_ORDER)}
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        source = str(item.get("source") or "mewcode")
        buckets.setdefault(source, []).append(item)
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in SOURCE_ORDER:
        seen.add(source)
        bucket = list(buckets.get(source) or [])
        bucket.sort(
            key=lambda item: str(item.get("updated_at") or item.get("last_active") or ""),
            reverse=True,
        )
        ordered.extend(bucket)
    for source in sorted(set(buckets) - seen):
        bucket = list(buckets[source])
        bucket.sort(
            key=lambda item: str(item.get("updated_at") or item.get("last_active") or ""),
            reverse=True,
        )
        ordered.extend(bucket)
    return ordered


def empty_sources() -> list[dict[str, Any]]:
    return [
        {"id": source, "label": source_label(source), "projects": []}
        for source in SOURCE_ORDER
    ]


def _conversation(
    *,
    item_id: str,
    title: str,
    source: str,
    writable: bool,
    scope: str = "",
    dir_path: str = "",
    external_id: str = "",
    node_id: str = "local",
    message_count: int = 0,
    last_active: str = "",
    active: bool = False,
    running: bool = False,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": title or "未命名对话",
        "source": source,
        "writable": writable,
        "scope": scope,
        "dir": dir_path,
        "external_id": external_id or item_id,
        "node_id": node_id,
        "message_count": message_count,
        "last_active": last_active,
        "active": active,
        "running": running,
    }


def _project(
    project_id: str,
    label: str,
    conversations: list[dict[str, Any]],
    *,
    dir_path: str = "",
    kind: str = "project",
) -> dict[str, Any]:
    ordered = sorted(
        conversations,
        key=lambda item: str(item.get("last_active") or ""),
        reverse=True,
    )
    return {
        "id": project_id,
        "label": label,
        "dir": dir_path,
        "kind": kind,
        "conversations": ordered,
    }


def _add_to_source(
    sources: dict[str, dict[str, dict[str, Any]]],
    source: str,
    project_id: str,
    label: str,
    conversation: dict[str, Any],
    *,
    dir_path: str = "",
    kind: str = "project",
) -> None:
    bucket = sources.setdefault(source, {})
    key = project_id or NORMAL_PROJECT_ID
    if key not in bucket:
        bucket[key] = {
            "id": key,
            "label": label or key,
            "dir": dir_path,
            "kind": kind,
            "conversations": [],
        }
    bucket[key]["conversations"].append(conversation)


def _finalize_sources(grouped: dict[str, dict[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    result = []
    for source in SOURCE_ORDER:
        projects_map = grouped.get(source, {})
        projects = []
        for special in (NORMAL_PROJECT_ID, PURE_CHAT_ID):
            if special in projects_map:
                item = projects_map[special]
                item["conversations"] = sorted(
                    item.get("conversations") or [],
                    key=lambda conv: str(conv.get("last_active") or ""),
                    reverse=True,
                )
                projects.append(item)
        for key, project in projects_map.items():
            if key in {NORMAL_PROJECT_ID, PURE_CHAT_ID}:
                continue
            project["conversations"] = sorted(
                project.get("conversations") or [],
                key=lambda conv: str(conv.get("last_active") or ""),
                reverse=True,
            )
            projects.append(project)
        result.append({"id": source, "label": source_label(source), "projects": projects})
    return result


def _external_is_project(item: dict[str, Any], folder_label: str, project_path: str) -> bool:
    if "is_project" in item:
        return bool(item.get("is_project"))
    if folder_label.startswith("未关联项目"):
        return False
    return bool(project_path.strip())


def local_machine(sidebar: dict[str, Any], external_items: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {source: {} for source in SOURCE_ORDER}
    for project in sidebar.get("projects") or []:
        dir_path = str(project.get("dir") or "")
        label = str(project.get("label") or Path(dir_path).name or dir_path or NORMAL_PROJECT_ID)
        conversations = []
        for item in project.get("conversations") or []:
            conversations.append(_conversation(
                item_id=str(item.get("id") or ""),
                title=str(item.get("title") or "未命名对话"),
                source="mewcode",
                writable=True,
                scope="project",
                dir_path=dir_path,
                message_count=int(item.get("message_count") or 0),
                last_active=str(item.get("last_active") or ""),
                active=bool(item.get("active")),
                running=bool(item.get("running")),
            ))
        if dir_path:
            grouped["mewcode"][dir_path] = _project(dir_path, label, conversations, dir_path=dir_path)
    normal_items = []
    for item in sidebar.get("normal") or []:
        normal_items.append(_conversation(
            item_id=str(item.get("id") or ""),
            title=str(item.get("title") or "未命名对话"),
            source="mewcode",
            writable=True,
            scope="user",
            message_count=int(item.get("message_count") or 0),
            last_active=str(item.get("last_active") or ""),
            active=bool(item.get("active")),
            running=bool(item.get("running")),
        ))
    grouped["mewcode"][NORMAL_PROJECT_ID] = _project(
        NORMAL_PROJECT_ID, NORMAL_PROJECT_ID, normal_items, kind="chats"
    )
    for item in external_items:
        source = str(item.get("source") or "")
        if source not in SOURCE_ORDER or source == "mewcode":
            continue
        if item.get("sidebar_visible") is False:
            continue
        project_path = str(item.get("project_path") or "").strip()
        folder_label = str(item.get("parent_folder") or project_path or "").strip()
        if folder_label and not folder_label.startswith("未关联项目"):
            folder_label = Path(folder_label).name or folder_label
        if _external_is_project(item, folder_label, project_path):
            project_id = project_path or folder_label
            kind = "project"
            label = folder_label or Path(project_path).name or project_id
        else:
            project_id = PURE_CHAT_ID
            kind = "chats"
            label = PURE_CHAT_ID
            project_path = ""
        _add_to_source(
            grouped,
            source,
            project_id,
            label,
            _conversation(
                item_id=str(item.get("external_id") or ""),
                title=str(item.get("title") or "未命名对话"),
                source=source,
                writable=False,
                external_id=str(item.get("external_id") or ""),
                message_count=int(item.get("message_count") or 0),
                last_active=str(item.get("updated_at") or item.get("last_active") or ""),
            ),
            dir_path=project_path,
            kind=kind,
        )
    return {
        "id": "local",
        "kind": "local",
        "label": "本机",
        "online": True,
        "error": None,
        "sources": _finalize_sources(grouped),
    }


def remote_machine(node: dict[str, Any], history: dict[str, Any] | None, error: str | None) -> dict[str, Any]:
    node_id = str(node.get("id") or "remote")
    label = str(node.get("hostname") or node.get("dns_name") or node.get("id") or "远程机").rstrip(".")
    grouped: dict[str, dict[str, dict[str, Any]]] = {source: {} for source in SOURCE_ORDER}
    for item in (history or {}).get("items") or []:
        source = str(item.get("source") or "mewcode")
        if source not in SOURCE_ORDER:
            source = "mewcode"
        if item.get("sidebar_visible") is False:
            continue
        project_path = str(item.get("project_path") or "").strip()
        folder_label = str(item.get("parent_folder") or project_path or "").strip()
        if folder_label and not folder_label.startswith("未关联项目"):
            folder_label = Path(folder_label).name or folder_label
        if source == "mewcode":
            project_id = folder_label or NORMAL_PROJECT_ID
            label = project_id
            kind = "chats" if project_id == NORMAL_PROJECT_ID else "project"
        elif _external_is_project(item, folder_label, project_path):
            project_id = project_path or folder_label
            label = folder_label or Path(project_path).name or project_id
            kind = "project"
        else:
            project_id = PURE_CHAT_ID
            label = PURE_CHAT_ID
            kind = "chats"
            project_path = ""
        writable = source == "mewcode" and bool(item.get("controllable"))
        _add_to_source(
            grouped,
            source,
            project_id,
            label,
            _conversation(
                item_id=str(item.get("external_id") or item.get("id") or ""),
                title=str(item.get("title") or "未命名对话"),
                source=source,
                writable=False,
                scope=str(item.get("scope") or ""),
                external_id=str(item.get("external_id") or item.get("id") or ""),
                node_id=node_id,
                message_count=int(item.get("message_count") or 0),
                last_active=str(item.get("updated_at") or item.get("last_active") or ""),
            ),
            dir_path=project_path,
            kind=kind,
        )
    return {
        "id": node_id,
        "kind": "remote",
        "label": label or "远程机",
        "online": error is None,
        "error": error,
        "token_configured": bool(node.get("token_configured")),
        "sources": _finalize_sources(grouped),
    }


def build_import_text(source: str, title: str, messages: list[dict[str, Any]]) -> tuple[str, bool]:
    label = source_label(source)
    speech = [
        message for message in (messages or [])
        if str(message.get("kind") or "message") == "message"
    ]
    selected = list(speech)[-MAX_IMPORT_MESSAGES:]
    truncated = len(speech) > MAX_IMPORT_MESSAGES
    lines = [f"从 {label} 对话「{title or '未命名对话'}」导入的只读上下文：", ""]
    used = 0
    kept: list[str] = []
    for message in selected:
        role = str(message.get("role") or "").upper() or "MESSAGE"
        text = str(message.get("text") or message.get("content") or "").strip()
        if not text:
            continue
        block = f"[{role}] {text}"
        if used + len(block) + 2 > MAX_IMPORT_CHARS:
            truncated = True
            break
        kept.append(block)
        used += len(block) + 2
    if not kept:
        kept.append("（没有可导入的正文）")
    lines.extend(kept)
    if truncated:
        lines.extend(["", "已截断"])
    return "\n".join(lines), truncated


def collect_workspace_tree(
    *,
    sidebar: dict[str, Any],
    external_items: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    fetch_history: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    machines = [local_machine(sidebar, external_items)]
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        error = None
        history: dict[str, Any] | None = None
        if not node.get("token_configured"):
            error = "缺少访问令牌"
        else:
            try:
                history = fetch_history(node_id)
            except Exception as exc:  # noqa: BLE001 — node errors stay on that branch
                error = str(exc) or "远程节点不可达"
        machines.append(remote_machine(node, history, error))
    return {"machines": machines, "work_dir": sidebar.get("work_dir") or ""}
