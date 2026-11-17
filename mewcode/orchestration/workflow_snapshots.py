"""Bounded, process-local immutable workflow reads; never writes session state."""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections import OrderedDict


def section_ranges(content):
    """Locate useful values in the exact cached JSON, without altering its body."""
    wanted = ("material_refresh.pending", "material_refresh.projection_repairs",
              "material_refresh.projection_repair_history", "pending", "pending_upserts",
              "document", "document.entries", "source_index", "sources", "material_refresh", "pending_history", "pending_contract")
    repair_sections = set(wanted[:5])
    decoder, ranges = json.JSONDecoder(), {}

    def whitespace(offset):
        while offset < len(content) and content[offset].isspace():
            offset += 1
        return offset

    def members(offset, prefix=""):
        offset = whitespace(offset + 1)
        while content[offset] != "}":
            key, end = decoder.raw_decode(content, offset)
            start = whitespace(whitespace(end) + 1)  # Skip the member's colon.
            value, end = decoder.raw_decode(content, start)
            path = prefix + "." + key if prefix else key
            if path in wanted and isinstance(value, (dict, list)) and (path not in repair_sections or value):
                ranges[path] = {"offset": start, "end": end}
            if path in {"document", "material_refresh"} and isinstance(value, dict):
                members(start, path)
            offset = whitespace(end)
            if content[offset] != ",":
                break
            offset = whitespace(offset + 1)

    if content.startswith("{"):
        members(0)
    # Put repair fields first in the compact header; the full JSON order stays fixed.
    return {path: ranges[path] for path in wanted if path in ranges}


class WorkflowSnapshotCache:
    def __init__(self, *, max_count=64, max_per_scope=8, max_bytes=32 * 1024 * 1024,
                 ttl=1800, clock=time.monotonic):
        self.max_count, self.max_per_scope, self.max_bytes = max_count, max_per_scope, max_bytes
        self.ttl, self.clock = ttl, clock
        self._snapshots, self._bases = OrderedDict(), OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    def _drop(self, key):
        self._bytes -= self._snapshots.pop(key)["bytes"]

    def _prune(self):
        cutoff = self.clock() - self.ttl
        for key, row in list(self._snapshots.items()):
            if row["created"] <= cutoff:
                self._drop(key)
        for key, row in list(self._bases.items()):
            if row["created"] <= cutoff:
                self._bases.pop(key)

    @staticmethod
    def _basis_key(scope, value):
        return (scope, value["sequence"], value["source_hash"], value["base_graph_hash"])

    def remember_basis(self, scope, value):
        with self._lock:
            self._prune()
            key = self._basis_key(scope, value)
            self._bases[key] = {"created": self.clock(), "value": copy.deepcopy(value)}
            self._bases.move_to_end(key)
            while len(self._bases) > self.max_count * 4:
                self._bases.popitem(last=False)

    def legacy_basis(self, scope, *, sequence, source_hash, base_graph_hash):
        with self._lock:
            self._prune()
            row = self._bases.get((scope, sequence, source_hash, base_graph_hash))
            return copy.deepcopy(row["value"]) if row else None

    def create(self, scope, value, sources, selection):
        content = json.dumps(value, ensure_ascii=False, sort_keys=True)
        # Count both the serialized page and the complete source ledger retained
        # for exact source_ids reads. Oversized histories fail without truncation.
        cost = len(content.encode("utf-8")) + len(json.dumps(sources, ensure_ascii=False).encode("utf-8"))
        if cost > self.max_bytes:
            raise ValueError("素材快照超过缓存上限，请按 entry_ids/query 缩小读取范围；未截断来源。")
        sections = section_ranges(content)
        with self._lock:
            self._prune()
            scoped = [key for key, row in self._snapshots.items() if row["scope"] == scope]
            while len(scoped) >= self.max_per_scope:
                self._drop(scoped.pop(0))
            while self._snapshots and (len(self._snapshots) >= self.max_count or self._bytes + cost > self.max_bytes):
                self._drop(next(iter(self._snapshots)))
            snapshot_id = "wbs_" + uuid.uuid4().hex
            self._snapshots[snapshot_id] = {"scope": scope, "created": self.clock(), "bytes": cost,
                "content": content, "value": copy.deepcopy(value), "sources": copy.deepcopy(sources),
                "selection": copy.deepcopy(selection), "section_ranges": sections}
            self._bytes += cost
            return snapshot_id

    def get(self, scope, snapshot_id):
        with self._lock:
            self._prune()
            row = self._snapshots.get(snapshot_id)
            if row is None:
                raise ValueError("SNAPSHOT_EXPIRED：素材快照已过期或宿主已重启，请从 offset=0 重新读取。")
            if row["scope"] != scope:
                raise ValueError("SNAPSHOT_SCOPE：快照不属于当前工作区与会话，请重新读取。")
            # Return a copy so a caller cannot alter subsequent pages or validation.
            return copy.deepcopy(row)


WORKFLOW_SNAPSHOTS = WorkflowSnapshotCache()
