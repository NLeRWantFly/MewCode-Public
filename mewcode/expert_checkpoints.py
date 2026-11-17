"""Immutable, compressed end-of-turn orchestration checkpoints.

These archive execution state and artifact references, never project files.
Conversation history remains append-only when a checkpoint is restored.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone


ID = re.compile(r"^[a-f0-9]{32}$")
STOPPED = frozenset({"completed", "blocked", "needs_attention", "paused", "terminated", "failed"})
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
RESTORE_FIELDS = (
    "node_ledgers", "ledger_migration", "branch_waits", "fallback_resolutions", "assistance_flow_revision",
    "review_queues", "review_queue_archives",
    "locked_flow", "flow_id", "flow_version", "flow_fingerprint", "finalizer_node_id",
    "locked_profile", "locked_models", "difficult_node_model_policy", "stage_cursor", "node_states", "stages",
    "node_attempts", "members", "pipeline_runs", "node_expansions", "handoffs", "handoff", "tools",
    "children", "audit_protocols", "loop_states", "loop_control_policy", "attachments", "gate_confirmations",
)


def _encode(value) -> bytes:
    # JSON escapes preserve even legacy lone surrogates without data loss.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    handle, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ExpertCheckpointArchive:
    def __init__(self, root: Path):
        self.root = Path(root)

    def folder(self, run_id: str) -> Path:
        if not ID.fullmatch(run_id):
            raise ValueError("invalid expert run id")
        folder = self.root / run_id
        if not folder.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("checkpoint path escapes archive")
        return folder

    def _safe_path(self, run_id: str, path: Path) -> Path:
        if not path.resolve().is_relative_to(self.folder(run_id).resolve()):
            raise ValueError("checkpoint path escapes run archive")
        return path

    def paths(self, run_id: str, checkpoint_id: str) -> tuple[Path, Path]:
        if not ID.fullmatch(checkpoint_id):
            raise ValueError("invalid checkpoint id")
        folder = self.folder(run_id)
        return (self._safe_path(run_id, folder / (checkpoint_id + ".json")),
                self._safe_path(run_id, folder / (checkpoint_id + ".json.gz")))

    def _blob(self, run_id: str, value: dict) -> str:
        data = _encode(value)
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ValueError("节点检查点超过安全大小上限")
        digest = hashlib.sha256(data).hexdigest()
        path = self._safe_path(run_id, self.folder(run_id) / "objects" / (digest + ".json.gz"))
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, gzip.compress(data, compresslevel=6, mtime=0))
        return digest

    def _read_blob(self, run_id: str, digest: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("invalid checkpoint object")
        path = self._safe_path(run_id, self.folder(run_id) / "objects" / (digest + ".json.gz"))
        with gzip.open(path, "rb") as stream:
            data = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(data) > MAX_SNAPSHOT_BYTES or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("节点检查点完整性校验失败")
        return json.loads(data)

    def save(self, snapshot: dict, *, turn_key: str, kind: str = "turn_end", node_id: str = "") -> dict:
        if snapshot.get("status") not in STOPPED and kind != "node_end":
            raise ValueError("运行中的任务不能保存为已结束对话检查点")
        run_id = str(snapshot["run_id"])
        checkpoint_id = hashlib.sha256((run_id + ":" + kind + ":" + turn_key).encode()).hexdigest()[:32]
        meta_path, data_path = self.paths(run_id, checkpoint_id)
        if meta_path.exists():
            meta, _ = self.load(run_id, checkpoint_id)
            return meta
        states = snapshot.get("node_states") or snapshot.get("stages") or {}
        if kind == "node_end" and (not node_id or (states.get(node_id) or {}).get("status") in {None, "running", "pending"}):
            raise ValueError("节点尚未结束，不能保存结束检查点")
        # Completed node conversations can be large. Share immutable per-node
        # objects between checkpoints rather than copying all prior turns.
        archived = {key: snapshot[key] for key in RESTORE_FIELDS if key in snapshot and key not in {"node_states", "stages"}}
        archived.update({key: snapshot.get(key) for key in (
            "run_id", "conversation_id", "status", "attempt", "updated_at", "completed_at", "last_seq",
        )})
        data = _encode({"archive_version": 1, "snapshot": archived,
                        "nodes": {key: self._blob(run_id, value) for key, value in states.items()}})
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ValueError("检查点超过安全大小上限，未覆盖已有版本")
        cursor = node_id or str(snapshot.get("stage_cursor") or "")
        node = next((item for item in (snapshot.get("locked_flow") or {}).get("nodes", [])
                     if item.get("node_id") == cursor), {})
        point_status = (states.get(node_id) or {}).get("status") if node_id else snapshot.get("status")
        if node_id and point_status == "completed" and (states.get(node_id) or {}).get("passed") is False:
            point_status = "needs_attention"
        meta = {
            "checkpoint_id": checkpoint_id, "run_id": run_id,
            "conversation_id": snapshot.get("conversation_id"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "kind": kind, "turn_key": turn_key, "attempt": snapshot.get("attempt", 1),
            "last_seq": snapshot.get("last_seq", 0), "status": point_status,
            "run_status": snapshot.get("status"), "node_id": node_id,
            "stage_cursor": cursor, "label": node.get("label") or cursor,
            "description": str(node.get("responsibility") or "")[:160],
            "completed_nodes": sum(state.get("status") == "completed" for state in states.values() if isinstance(state, dict)),
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        }
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        # Publish metadata last: incomplete writes are never offered for restore.
        _atomic_write(data_path, gzip.compress(data, compresslevel=6, mtime=0))
        _atomic_write(meta_path, _encode(meta))
        return meta

    def load(self, run_id: str, checkpoint_id: str) -> tuple[dict, dict]:
        meta_path, data_path = self.paths(run_id, checkpoint_id)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        with gzip.open(data_path, "rb") as stream:
            data = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(data) > MAX_SNAPSHOT_BYTES or hashlib.sha256(data).hexdigest() != meta.get("sha256"):
            raise ValueError("检查点完整性校验失败，未恢复进度")
        archived = json.loads(data)
        if archived.get("archive_version") != 1:
            raise ValueError("不支持的检查点版本")
        snapshot = archived["snapshot"]
        snapshot["node_states"] = {key: self._read_blob(run_id, digest) for key, digest in archived["nodes"].items()}
        if (meta.get("run_id") != run_id or meta.get("checkpoint_id") != checkpoint_id
                or snapshot.get("run_id") != run_id
                or snapshot.get("conversation_id") != meta.get("conversation_id")):
            raise ValueError("检查点不属于该任务")
        return meta, snapshot

    def delete(self, run_id: str) -> None:
        folder = self.folder(run_id)
        if not folder.exists():
            return
        # Only this run's archive files; never recurse into unrelated content.
        for path in folder.glob("*.json*"):
            if ID.fullmatch(path.name.split(".", 1)[0]) and path.suffix in {".json", ".gz"}:
                self._safe_path(run_id, path).unlink()
        objects = self._safe_path(run_id, folder / "objects")
        if objects.is_dir():
            for path in objects.glob("*.json.gz"):
                if re.fullmatch(r"[a-f0-9]{64}\.json\.gz", path.name):
                    self._safe_path(run_id, path).unlink()
            if not any(objects.iterdir()):
                objects.rmdir()
        if not any(folder.iterdir()):
            folder.rmdir()

    def list(self, run_id: str) -> list[dict]:
        items = []
        for path in self.folder(run_id).glob("*.json"):
            if not ID.fullmatch(path.stem):
                continue
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
                if (meta.get("run_id") == run_id and meta.get("checkpoint_id") == path.stem
                        and path.with_suffix(".json.gz").is_file()):
                    items.append(meta)
            except (OSError, ValueError, TypeError):
                continue
        return sorted(items, key=lambda meta: meta["created_at"], reverse=True)
