"""Conversation-owned views over the legacy project-keyed plan/flow stores.

The hashed child path is a JSON store key, not a directory we create. Legacy
project records remain intact and are readable only with explicit ownership.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from mewcode.orchestration.spec import OrchestrationError


class ConversationStore:
    def __init__(self, store, conversation_id: str):
        self.store = store
        self.conversation_id = str(conversation_id or "")

    def key(self, project):
        if not self.conversation_id:
            return project  # Non-chat/CLI project configuration remains supported.
        digest = hashlib.sha256(self.conversation_id.encode()).hexdigest()
        return Path(project) / ".mewcode" / "conversation-store-keys" / digest

    def get(self, project):
        value = self.store.get(self.key(project))
        if value is None and self.conversation_id:
            value = self.store.get(project)
        if value is not None and self.conversation_id:
            spec = getattr(value, "spec", value)
            if str(getattr(spec, "meta", {}).get("conversation_id") or "") != self.conversation_id:
                return None
        return value

    def put(self, project, value):
        if self.conversation_id:
            data = value.as_dict() if hasattr(value, "as_dict") else dict(value)
            data = dict(data)
            if "spec" in data and not isinstance(data["spec"], dict):
                raise OrchestrationError("当前对话的方案缺少阶段图")
            spec = dict(data["spec"]) if "spec" in data else data
            meta = dict(spec.get("meta") or {})
            owner = str(meta.get("conversation_id") or "")
            if owner and owner != self.conversation_id:
                raise OrchestrationError("方案属于另一个对话，请重新打开当前对话的编排")
            meta["conversation_id"] = self.conversation_id
            spec["meta"] = meta
            if "spec" in data:
                data["spec"] = spec
            value = data
        return self.store.put(self.key(project), value)

    def delete(self, project):
        self.store.delete(self.key(project))
