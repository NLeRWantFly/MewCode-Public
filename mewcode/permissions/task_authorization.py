"""Host-owned, versioned task scopes for deterministic tool decisions.

This is never inferred from a model verdict. A UI/API caller must explicitly
provide the directories, tools, and expiry; semantic review cannot mutate it.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mewcode.permissions.rules import extract_content

_PATH_TOOLS = frozenset({"ReadFile", "WriteFile", "EditFile", "ReportArtifact"})
_NETWORK_TOOLS = frozenset({"WebFetch", "web_fetch"})
_BEHAVIORS = frozenset({"isolated_command", "git_write"})


def _directory(value: str) -> str:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ValueError("任务授权目录必须是绝对路径")
    try:
        path = raw.resolve(strict=True)
    except OSError as exc:
        raise ValueError("任务授权需要现有的具体目录：" + str(raw)) from exc
    if not path.is_dir() or path == Path(path.anchor):
        raise ValueError("任务授权需要现有的具体目录，不能授权整个磁盘")
    return str(path)


def _output_directory(value: str) -> str:
    """A directory this task may create, not necessarily one that exists yet.

    Authorizing an output root before it exists is normal work, so the nearest
    existing ancestor is what has to be a real directory. The anchor is still
    refused, relative paths are still refused, and the resolved path is what gets
    recorded, so a symlinked parent cannot widen the granted root.
    """
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ValueError("任务授权目录必须是绝对路径")
    path = raw.resolve(strict=False)
    if path == Path(path.anchor):
        raise ValueError("任务授权需要具体的输出目录，不能授权整个磁盘")
    ancestor = path
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise ValueError("任务授权需要现有目录作为输出目录的上级")
    return str(path)


@dataclass(frozen=True)
class TaskAuthorization:
    task_id: str
    version: str
    read_directories: tuple[str, ...]
    write_directories: tuple[str, ...]
    network_hosts: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    allowed_behaviors: tuple[str, ...]
    expires_at: float

    @classmethod
    def from_user_scope(cls, payload: dict[str, Any], *, task_id: str) -> "TaskAuthorization":
        if not task_id:
            raise ValueError("缺少当前任务 ID")
        for field in ("read_directories", "write_directories", "network_hosts", "allowed_tools", "allowed_behaviors"):
            if not isinstance(payload.get(field, []), list):
                raise ValueError(f"{field} 必须是列表")
        read = tuple(sorted({_directory(str(item)) for item in payload.get("read_directories", [])}))
        write = tuple(sorted({_output_directory(str(item)) for item in payload.get("write_directories", [])}))
        hosts = tuple(sorted({str(item).lower().strip() for item in payload.get("network_hosts", [])}))
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]|[a-z0-9]", host)
               for host in hosts):
            raise ValueError("网络目标必须是精确主机名，不允许通配符")
        tools = tuple(sorted({str(item).strip() for item in payload.get("allowed_tools", [])}))
        if any(item not in _PATH_TOOLS | _NETWORK_TOOLS for item in tools):
            raise ValueError("任务授权仅支持明确目标的文件工具和 WebFetch")
        behaviors = tuple(sorted({str(item).strip() for item in payload.get("allowed_behaviors", [])}))
        if any(item not in _BEHAVIORS for item in behaviors):
            raise ValueError("任务授权包含不支持的执行行为")
        ttl = int(payload.get("ttl_seconds", 3600))
        if not 60 <= ttl <= 86400:
            raise ValueError("任务授权有效期须为 60 秒至 24 小时")
        expires = time.time() + ttl
        fields = [task_id, read, write, hosts, tools, behaviors, expires]
        version = hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()[:16]
        return cls(task_id, version, read, write, hosts, tools, behaviors, expires)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "version": self.version,
            "read_directories": list(self.read_directories),
            "write_directories": list(self.write_directories),
            "network_hosts": list(self.network_hosts),
            "allowed_tools": list(self.allowed_tools),
            "allowed_behaviors": list(self.allowed_behaviors),
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "TaskAuthorization":
        task_id = str(record.get("task_id") or "")
        for field in ("read_directories", "write_directories", "network_hosts", "allowed_tools", "allowed_behaviors"):
            if not isinstance(record.get(field, []), list):
                raise ValueError("任务授权记录格式错误")
        read = tuple(_directory(str(item)) for item in record.get("read_directories", []))
        write = tuple(_output_directory(str(item)) for item in record.get("write_directories", []))
        hosts = tuple(str(item) for item in record.get("network_hosts", []))
        tools = tuple(str(item) for item in record.get("allowed_tools", []))
        behaviors = tuple(str(item) for item in record.get("allowed_behaviors", []))
        if any(item not in _PATH_TOOLS | _NETWORK_TOOLS for item in tools):
            raise ValueError("任务授权包含不支持的工具")
        if any(item not in _BEHAVIORS for item in behaviors):
            raise ValueError("任务授权包含不支持的执行行为")
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]|[a-z0-9]", host)
               for host in hosts):
            raise ValueError("任务授权网络目标无效")
        expires = float(record.get("expires_at", 0))
        version = hashlib.sha256(json.dumps(
            [task_id, read, write, hosts, tools, behaviors, expires], ensure_ascii=False,
        ).encode()).hexdigest()[:16]
        if (version != record.get("version") or not math.isfinite(expires)
                or time.time() >= expires or expires > time.time() + 86460):
            raise ValueError("任务授权已过期或版本不匹配")
        return cls(task_id, version, read, write, hosts, tools, behaviors, expires)

    def covers(self, tool: Any, arguments: dict[str, Any], *, task_id: str) -> bool:
        if task_id != self.task_id or time.time() >= self.expires_at or tool.name not in self.allowed_tools:
            return False
        if tool.name in _NETWORK_TOOLS and self.network_hosts:
            url = arguments.get("url")
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                host = parsed.hostname if parsed else None
                port = parsed.port if parsed else None
            except ValueError:
                return False
            default_port = 443 if parsed and parsed.scheme == "https" else 80
            if (parsed and parsed.scheme in {"http", "https"} and not parsed.username
                    and not parsed.password and port in {None, default_port}
                    and host and host.lower() in self.network_hosts):
                return True
        if tool.name in _PATH_TOOLS and tool.category in {"read", "write"}:
            raw = extract_content(tool.name, arguments)
            if not raw or any(char in raw for char in "*?[]"):
                return False
            target = Path(raw).expanduser()
            if not target.is_absolute():
                return False
            try:
                resolved = target.resolve(strict=False)
            except (OSError, RuntimeError):
                return False
            roots = self.read_directories if tool.category == "read" else self.write_directories
            return any(resolved.is_relative_to(Path(root)) for root in roots)
        return False
