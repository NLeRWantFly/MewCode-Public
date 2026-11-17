from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from mewcode.teams.fsio import atomic_write_text, quarantine_corrupt_file
from mewcode.teams.progress import TeammateProgress


class TeamConfigError(Exception):
    """config.json 损坏或不可解析。原文件已被隔离备份。"""


class BackendType(str, Enum):
    TMUX = "tmux"
    ITERM2 = "iterm2"
    IN_PROCESS = "in-process"


@dataclass
class TeammateInfo:
    name: str
    agent_id: str
    agent_type: str
    model: str
    worktree_path: str
    backend_type: str  # BackendType value
    is_active: bool | None = None
    progress: Optional[TeammateProgress] = None

    def to_dict(self) -> dict:
        # Exclude progress (runtime-only, contains threading.Lock)
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "model": self.model,
            "worktree_path": self.worktree_path,
            "backend_type": self.backend_type,
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TeammateInfo:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _sanitize_name(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "team"


@dataclass
class AgentTeam:
    name: str
    lead_agent_id: str
    members: list[TeammateInfo] = field(default_factory=list)
    config_path: str = ""
    description: str = ""
    # 崩溃恢复线索：lead 进程 PID 与创建时间。进程死后 is_active 残留
    # True 时，据此判断团队已成僵尸、允许 force 清理
    lead_pid: int = 0
    created_at: float = 0.0

    def get_member(self, name: str) -> TeammateInfo | None:
        for m in self.members:
            if m.name == name or m.agent_id == name:
                return m
        return None


    def add_member(self, member: TeammateInfo) -> None:
        self.members.append(member)

    def remove_member(self, name: str) -> bool:
        for i, m in enumerate(self.members):
            if m.name == name or m.agent_id == name:
                self.members.pop(i)
                return True
        return False


    def set_member_active(self, name: str, is_active: bool | None) -> bool:
        member = self.get_member(name)
        if member is None:
            return False
        member.is_active = is_active
        return True

    def all_idle(self) -> bool:
        return all(m.is_active is False for m in self.members)


    def active_members(self) -> list[TeammateInfo]:
        return [m for m in self.members if m.is_active is not False]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lead_agent_id": self.lead_agent_id,
            "members": [m.to_dict() for m in self.members],
            "config_path": self.config_path,
            "description": self.description,
            "lead_pid": self.lead_pid,
            "created_at": self.created_at,
        }


    @classmethod
    def from_dict(cls, data: dict) -> AgentTeam:
        members = [TeammateInfo.from_dict(m) for m in data.get("members", [])]
        return cls(
            name=data["name"],
            lead_agent_id=data["lead_agent_id"],
            members=members,
            config_path=data.get("config_path", ""),
            description=data.get("description", ""),
            lead_pid=data.get("lead_pid", 0),
            created_at=data.get("created_at", 0.0),
        )

    def save(self) -> None:
        path = Path(self.config_path)
        atomic_write_text(
            path, json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        )

    @classmethod
    def load(cls, config_path: str) -> AgentTeam:
        path = Path(config_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # 半写/损坏的 config 不隔离的话，团队会既用不了也删不掉
            backup = quarantine_corrupt_file(path)
            raise TeamConfigError(
                f"Corrupt team config {config_path}"
                + (f" (backed up to {backup})" if backup else "")
            ) from e
        team = cls.from_dict(data)
        team.config_path = config_path
        return team


def resolve_team_dir(team_name: str) -> Path:
    slug = _sanitize_name(team_name)
    return Path.home() / ".mewcode" / "teams" / slug


def unique_team_name(team_name: str) -> str:
    slug = _sanitize_name(team_name)
    base_dir = Path.home() / ".mewcode" / "teams"
    if not (base_dir / slug).exists():
        return slug
    counter = 2
    while (base_dir / f"{slug}-{counter}").exists():
        counter += 1
    return f"{slug}-{counter}"
