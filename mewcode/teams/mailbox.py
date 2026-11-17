from __future__ import annotations

import itertools
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 进程内单调序列：time.time() 分辨率内连续写入会产生相同时间戳前缀，
# 仅按文件名排序会打乱 FIFO——序列号作为同时间戳的决胜位
_write_seq = itertools.count()


@dataclass
class MailboxMessage:
    id: str
    from_agent: str
    to_agent: str
    content: str
    summary: str = ""
    message_type: str = "text"  # text | shutdown_request | shutdown_response
    timestamp: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MailboxMessage:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class Mailbox:
    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)

    def _agent_dir(self, agent_id: str) -> Path:
        return self._base_dir / agent_id


    def write(self, agent_id: str, message: MailboxMessage) -> None:
        from mewcode.teams.fsio import atomic_write_text

        d = self._agent_dir(agent_id)
        filename = f"{message.timestamp:.6f}_{next(_write_seq):06d}_{message.id}.json"
        atomic_write_text(
            d / filename, json.dumps(message.to_dict(), ensure_ascii=False)
        )

    def read(self, agent_id: str) -> list[MailboxMessage]:
        d = self._agent_dir(agent_id)
        if not d.exists():
            return []
        messages: list[MailboxMessage] = []
        for f in sorted(d.iterdir()):
            if f.suffix != ".json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                messages.append(MailboxMessage.from_dict(data))
            except (json.JSONDecodeError, KeyError):
                continue
        return messages

    def consume(self, agent_id: str) -> list[MailboxMessage]:
        d = self._agent_dir(agent_id)
        if not d.exists():
            return []
        messages: list[MailboxMessage] = []
        for f in sorted(d.iterdir()):
            if f.suffix != ".json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                msg = MailboxMessage.from_dict(data)
            except (json.JSONDecodeError, KeyError, OSError):
                continue
            try:
                f.unlink()
            except OSError:
                # Windows 上文件被并发持有时 unlink 会失败；
                # 宁可下轮重复消费也不要让整批消息丢失
                continue
            messages.append(msg)
        return messages

    def broadcast(
        self,
        team_members: list[str],
        message: MailboxMessage,
        exclude: str = "",
    ) -> None:
        for agent_id in team_members:
            if agent_id == exclude:
                continue
            self.write(agent_id, message)


    def cleanup(self, agent_id: str) -> None:
        d = self._agent_dir(agent_id)
        if d.exists():
            for f in d.iterdir():
                f.unlink(missing_ok=True)
            d.rmdir()

    def cleanup_all(self) -> None:
        if not self._base_dir.exists():
            return
        for d in self._base_dir.iterdir():
            if d.is_dir():
                for f in d.iterdir():
                    f.unlink(missing_ok=True)
                d.rmdir()


def create_message(
    from_agent: str,
    to_agent: str,
    content: str,
    summary: str = "",
    message_type: str = "text",
    metadata: dict[str, Any] | None = None,
) -> MailboxMessage:
    return MailboxMessage(
        id=uuid.uuid4().hex[:12],
        from_agent=from_agent,
        to_agent=to_agent,
        content=content,
        summary=summary,
        message_type=message_type,
        timestamp=time.time(),
        metadata=metadata or {},
    )
