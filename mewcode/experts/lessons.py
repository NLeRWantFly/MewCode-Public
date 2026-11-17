"""User-approved flow lessons. Reflection may propose, never activate."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from mewcode.runtime_paths import user_data_root

_LOCK = threading.RLock()


class FlowLessonStore:
    def __init__(self, path=None):
        self.path = Path(path or user_data_root() / "flow-lessons.json")

    def _load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError("Invalid flow lesson store")
        return value

    def _write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".flow-lessons-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(value, out, ensure_ascii=False, indent=2)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _rows(data, flow_id):
        for owner in dict.fromkeys([flow_id, "*"]):
            for scope, items in data.get(owner, {}).items():
                for item in items:
                    yield owner, scope, item

    def list(self, flow_id):
        with _LOCK:
            return [{**copy.deepcopy(item), "flow_id": owner, "scope": scope}
                    for owner, scope, item in self._rows(self._load(), flow_id)]

    def add(self, flow_id, scope, text, *, source="user", run_id="", why=""):
        text = str(text).strip()
        if not text or len(text) > 2000 or not flow_id or len(flow_id) > 200 or not scope or len(scope) > 200:
            raise ValueError("经验需要有效的范围和 1–2000 字文本")
        if source not in {"user", "reflection"}:
            raise ValueError("Invalid lesson source")
        status = "pending" if source == "reflection" else "active"
        with _LOCK:
            data = self._load()
            items = data.setdefault(flow_id, {}).setdefault(scope, [])
            for item in items:
                if item["text"] == text and item["source"] == source and item["status"] == status:
                    return {**copy.deepcopy(item), "flow_id": flow_id, "scope": scope}
            item = {"lesson_id": uuid.uuid4().hex, "text": text, "source": source, "status": status,
                    "created_at": time.time(), "run_id": run_id, "hits": 0, "success_hits": 0,
                    "last_hit_at": None, "why": str(why)[:1000]}
            items.append(item)
            self._write(data)
            return {**copy.deepcopy(item), "flow_id": flow_id, "scope": scope}

    def update(self, flow_id, lesson_id, *, status=None, text=None, delete=False):
        if status is not None and status not in {"pending", "active", "disabled"}:
            raise ValueError("Invalid lesson status")
        if text is not None and (not str(text).strip() or len(str(text)) > 2000):
            raise ValueError("经验文本需要 1–2000 字")
        with _LOCK:
            data = self._load()
            for items in data.get(flow_id, {}).values():
                for item in list(items):
                    if item["lesson_id"] != lesson_id:
                        continue
                    if delete:
                        items.remove(item)
                    else:
                        if status is not None:
                            item["status"] = status
                        if text is not None:
                            item["text"] = str(text).strip()
                    self._write(data)
                    return copy.deepcopy(item)
            raise KeyError(lesson_id)

    def select(self, flow_id, node_id, role, limit=8):
        if limit <= 0:
            return []
        with _LOCK:
            data = self._load()
            matches = [(owner, scope, item) for owner, scope, item in self._rows(data, flow_id)
                       if item["status"] == "active" and scope in {"*", node_id, "role:" + role}]
            matches.sort(key=lambda row: (row[0] != flow_id, -row[2].get("hits", 0), -row[2]["created_at"]))
            chosen, seen = [], set()
            for owner, scope, item in matches:
                if item["text"] in seen:
                    continue
                seen.add(item["text"])
                item["hits"] = item.get("hits", 0) + 1
                item["last_hit_at"] = time.time()
                chosen.append({**copy.deepcopy(item), "flow_id": owner, "scope": scope})
                if len(chosen) >= min(8, limit):
                    break
            if chosen:
                self._write(data)
            return chosen

    def record_success(self, lessons):
        if not lessons:
            return
        with _LOCK:
            data = self._load()
            for used in lessons:
                for item in data.get(used["flow_id"], {}).get(used["scope"], []):
                    if item["lesson_id"] == used["lesson_id"]:
                        item["success_hits"] = min(item.get("hits", 0), item.get("success_hits", 0) + 1)
            self._write(data)


def user_lesson_from_supplemental(text):
    match = re.match(r"^(?:记住|以后|经验)\s*[:：]\s*(?:@([\w.-]+)\s+)?(.+)$", str(text).strip(), re.S)
    return (match.group(1) or "*", match.group(2).strip()) if match else None
