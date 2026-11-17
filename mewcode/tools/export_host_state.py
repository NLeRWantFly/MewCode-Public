"""Read-only, allowlisted export of one host-owned review queue.

The export intentionally does not serialize an expert-run snapshot or the
review queue's raw evidence, messages, paths, or business text.  It is a small
index for workspace scripts that need stable queue state and candidate
bindings.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mewcode.tools.base import Tool, ToolResult


_SHA256 = set("0123456789abcdef")
_RECORD_STATES = {"complete", "needs_repair", "unknown"}
_BATCH_STATES = {"frozen", "submitted", "released"}
_MAX_ITEMS = 10_000
_MAX_BATCHES = 10_000
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _identifier(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 256
            or value in {".", ".."} or "/" in value or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"{label}无效")
    return value


def _sha256(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in _SHA256 for char in value)):
        raise ValueError(f"{label}必须是小写 SHA256")
    return value


def _hashes(receipts: Any, label: str) -> list[str]:
    """Return receipt content digests only; never copy receipt bodies/paths."""
    if receipts is None:
        return []
    values = list(receipts.values()) if isinstance(receipts, dict) else receipts
    if not isinstance(values, list):
        raise ValueError(f"{label}结构无效")
    result = []
    for receipt in values:
        if not isinstance(receipt, dict):
            raise ValueError(f"{label}回执结构无效")
        value = receipt.get("sha256")
        if value is None:
            raise ValueError(f"{label}缺少 sha256")
        result.append(_sha256(value, f"{label}.sha256"))
    return sorted(result)


@dataclass(frozen=True)
class HostStateExportResult:
    snapshot_path: Path
    checksum_path: Path
    sha256: str
    queue_revision: int
    queue_fingerprint: str


class HostStateExporter:
    """Export one review queue without mutating its host run.

    Integration API::

        exporter = HostStateExporter(store, run_id, reviewer_node_id, root,
                                     contract_hash=expected_contract_hash)
        result = exporter.export()

    ``store.load_review_queue`` is required.  Falling back to ``store.load``
    would hydrate a private, full run snapshot and is deliberately unsupported.
    """

    def __init__(self, store: Any, run_id: str, reviewer_node_id: str,
                 root: str | Path, *, contract_hash: str | None = None):
        self.store = store
        self.run_id = _identifier(run_id, "run_id")
        self.reviewer_node_id = _identifier(reviewer_node_id, "reviewer_node_id")
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace root 必须是目录")
        self.expected_contract_hash = contract_hash

    def _load_queue(self) -> dict[str, Any]:
        loader = getattr(self.store, "load_review_queue", None)
        if not callable(loader):
            raise ValueError("宿主 store 必须提供只读 load_review_queue 接口")
        queue = loader(self.run_id, self.reviewer_node_id)
        if not isinstance(queue, dict):
            raise ValueError("宿主审核队列不存在或无法读取")
        return queue

    def build_payload(self) -> dict[str, Any]:
        """Build the bounded JSON projection; does not write or mutate state."""
        queue = self._load_queue()
        revision = queue.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("审核队列 revision 无效")
        contract_hash = _sha256(queue.get("contract_hash"), "contract_hash")
        if (self.expected_contract_hash is not None
                and contract_hash != _sha256(self.expected_contract_hash, "expected contract_hash")):
            raise ValueError("审核队列合同与请求合同不一致")
        manifest_sha256 = _sha256(queue.get("manifest_sha256"), "manifest_sha256")

        source_items = queue.get("items")
        records = queue.get("records") or {}
        candidates = queue.get("repair_candidates") or {}
        batches = queue.get("repair_batches") or {}
        if (not isinstance(source_items, list) or len(source_items) > _MAX_ITEMS
                or not isinstance(records, dict) or not isinstance(candidates, dict)
                or not isinstance(batches, dict) or len(batches) > _MAX_BATCHES):
            raise ValueError("审核队列超出导出范围或结构无效")

        item_ids: set[str] = set()
        item_rows = []
        receipt_fingerprints: dict[str, dict[str, list[str]]] = {}
        text_receipts = queue.get("text_receipts") or {}
        image_receipts = queue.get("image_receipts") or {}
        if not isinstance(text_receipts, dict) or not isinstance(image_receipts, dict):
            raise ValueError("审核回执索引结构无效")

        for item in source_items:
            if not isinstance(item, dict):
                raise ValueError("审核队列条目结构无效")
            item_id = _identifier(item.get("item_id"), "item_id")
            if item_id in item_ids:
                raise ValueError("审核队列存在重复 item_id")
            item_ids.add(item_id)
            record = records.get(item_id)
            if record is not None and not isinstance(record, dict):
                raise ValueError("审核记录结构无效")
            state = "unrecorded"
            evidence_hashes: list[str] = []
            image_hashes: list[str] = []
            rendered_hashes: list[str] = []
            if record is not None:
                state = record.get("state")
                if state not in _RECORD_STATES:
                    raise ValueError("审核记录状态无效")
                evidence_hashes = _hashes(record.get("evidence", []), "evidence")
                image_hashes = _hashes(record.get("images", []), "images")
                rendered_hashes = _hashes(record.get("rendered_images", []), "rendered_images")

            supplemental_text = _hashes(text_receipts.get(item_id, {}), "text_receipts")
            supplemental_images = _hashes(image_receipts.get(item_id, []), "image_receipts")
            receipt_fingerprints[item_id] = {
                "evidence": evidence_hashes,
                "images": image_hashes,
                "rendered_images": rendered_hashes,
                "text_receipts": supplemental_text,
                "image_receipts": supplemental_images,
            }
            candidate = candidates.get(item_id)
            binding = None
            if candidate is not None:
                if not isinstance(candidate, dict):
                    raise ValueError("候选绑定结构无效")
                binding = {
                    "candidate_revision": _sha256(candidate.get("candidate_revision"), "candidate_revision"),
                    "batch_id": _identifier(candidate.get("batch_id"), "candidate.batch_id"),
                    "status": _identifier(candidate.get("status", "unknown"), "candidate.status"),
                }
            item_rows.append({
                "item_id": item_id,
                "state": state,
                "recorded": record is not None,
                "candidate_binding": binding,
                "receipt_counts": {
                    "evidence": len(evidence_hashes),
                    "images": len(image_hashes),
                    "rendered_images": len(rendered_hashes),
                    "text_receipts": len(supplemental_text),
                    "image_receipts": len(supplemental_images),
                },
            })
        if set(records) - item_ids or set(candidates) - item_ids:
            raise ValueError("审核记录或候选绑定引用了队列外条目")

        item_rows.sort(key=lambda row: row["item_id"])
        batch_rows = []
        for batch_id, batch in batches.items():
            batch_id = _identifier(batch_id, "batch_id")
            if not isinstance(batch, dict):
                raise ValueError("返修批次结构无效")
            status = batch.get("status")
            if status not in _BATCH_STATES:
                raise ValueError("返修批次状态无效")
            raw_item_ids = batch.get("item_ids")
            if raw_item_ids is None:
                batch_item_rows = batch.get("items", [])
                if not isinstance(batch_item_rows, list) or any(
                        not isinstance(row, dict) for row in batch_item_rows):
                    raise ValueError("返修批次 items 无效")
                raw_item_ids = [row.get("item_id") for row in batch_item_rows]
            if not isinstance(raw_item_ids, list) or len(raw_item_ids) > 10:
                raise ValueError("返修批次 item_ids 无效")
            batch_items = sorted({_identifier(value, "batch.item_id") for value in raw_item_ids})
            if len(batch_items) != len(raw_item_ids) or not set(batch_items).issubset(item_ids):
                raise ValueError("返修批次包含重复或队列外条目")
            batch_candidate_bindings = []
            batch_candidates = batch.get("candidates") or {}
            if not isinstance(batch_candidates, dict):
                raise ValueError("批次候选绑定结构无效")
            for item_id, candidate in sorted(batch_candidates.items()):
                item_id = _identifier(item_id, "batch.candidate.item_id")
                if item_id not in item_ids or not isinstance(candidate, dict):
                    raise ValueError("批次候选绑定引用了无效条目")
                batch_candidate_bindings.append({
                    "item_id": item_id,
                    "candidate_revision": _sha256(candidate.get("candidate_revision"), "batch candidate_revision"),
                    "status": _identifier(candidate.get("status", "unknown"), "batch candidate.status"),
                })
            batch_revision = batch.get("revision")
            if (batch_revision is not None and
                    (isinstance(batch_revision, bool) or not isinstance(batch_revision, int)
                     or batch_revision < 0)):
                raise ValueError("返修批次 revision 无效")
            batch_rows.append({
                "batch_id": batch_id,
                "status": status,
                "revision": batch.get("revision") if isinstance(batch.get("revision"), int) else None,
                "item_ids": batch_items,
                "manifest_sha256": _sha256(batch.get("manifest_sha256"), "batch.manifest_sha256"),
                "candidate_bindings": batch_candidate_bindings,
            })
        batch_rows.sort(key=lambda row: row["batch_id"])
        if set(text_receipts) - item_ids or set(image_receipts) - item_ids:
            raise ValueError("审核回执索引引用了队列外条目")

        counts = {state: sum(row["state"] == state for row in item_rows)
                  for state in (*sorted(_RECORD_STATES), "unrecorded")}
        projected = {
            "schema_version": 1,
            "run_id": self.run_id,
            "reviewer_node_id": self.reviewer_node_id,
            "queue": {
                "revision": revision,
                "contract_hash": contract_hash,
                "manifest_sha256": manifest_sha256,
                "item_count": len(item_rows),
            },
            "summary": counts,
            "items": item_rows,
            "repair_batches": batch_rows,
        }
        # Include opaque receipt hashes in the fingerprint so changed evidence
        # invalidates consumers even though raw paths, quotes, and payloads are
        # intentionally omitted from the export.
        fingerprint_material = {**projected, "_receipt_fingerprints": receipt_fingerprints}
        projected["queue"]["fingerprint"] = _digest(fingerprint_material)
        return projected

    def _output_directory(self) -> Path:
        root = self.root.resolve(strict=True)
        reports = root / "reports"
        output = reports / "host_snapshot"
        # Check existing ancestors before mkdir, so a pre-planted junction or
        # symlink cannot redirect directory creation outside the workspace.
        for path in (reports, output):
            if path.exists() or path.is_symlink():
                if not path.resolve(strict=True).is_relative_to(root):
                    raise ValueError("host_snapshot 输出路径必须位于工作区内")
        output.mkdir(parents=True, exist_ok=True)
        resolved = output.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_dir():
            raise ValueError("host_snapshot 输出路径必须位于工作区内")
        return resolved

    @staticmethod
    def _atomic_write(path: Path, payload: bytes, root: Path) -> None:
        if path.is_symlink():
            raise ValueError("拒绝覆盖符号链接输出文件")
        if not path.resolve(strict=False).is_relative_to(root):
            raise ValueError("输出文件必须位于工作区内")
        fd, name = tempfile.mkstemp(prefix=".host-snapshot-", suffix=".tmp", dir=path.parent)
        temp_path = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if not temp_path.resolve(strict=True).is_relative_to(root):
                raise ValueError("临时输出文件必须位于工作区内")
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def export(self) -> HostStateExportResult:
        payload = self.build_payload()
        body = (json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(body) > _MAX_OUTPUT_BYTES:
            raise ValueError("host_snapshot 超过大小上限")
        digest = hashlib.sha256(body).hexdigest()
        directory = self._output_directory()
        snapshot_path = directory / "host_snapshot.json"
        checksum_path = directory / "host_snapshot.sha256"
        checksum = f"{digest}  {snapshot_path.name}\n".encode("ascii")
        self._atomic_write(snapshot_path, body, self.root)
        self._atomic_write(checksum_path, checksum, self.root)
        return HostStateExportResult(snapshot_path, checksum_path, digest,
                                     payload["queue"]["revision"],
                                     payload["queue"]["fingerprint"])


def export_host_state(store: Any, run_id: str, reviewer_node_id: str,
                      root: str | Path, *, contract_hash: str | None = None) -> HostStateExportResult:
    """Convenience integration function for the host tool registry."""
    return HostStateExporter(store, run_id, reviewer_node_id, root,
                             contract_hash=contract_hash).export()


class ExportHostStateParams(BaseModel):
    """The destination and queue are host-bound, not model-selectable."""


class ExportHostState(Tool):
    name = "ExportHostState"
    category = "write"
    is_system_tool = True
    params_model = ExportHostStateParams
    description = (
        "导出当前审阅队列的只读宿主状态投影到 reports/host_snapshot/；"
        "输出只含队列版本、指纹、条目状态和候选绑定，不读取业务原文或修改宿主台账。"
    )

    def __init__(self, store: Any, run_id: str, reviewer_node_id: str,
                 root: str | Path, *, contract_hash: str):
        self.exporter = HostStateExporter(store, run_id, reviewer_node_id, root,
                                          contract_hash=contract_hash)
        self.root = self.exporter.root

    async def execute(self, params: ExportHostStateParams) -> ToolResult:
        try:
            result = self.exporter.export()
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Host-state export failed closed: {exc}", is_error=True)
        return ToolResult(output=json.dumps({
            "snapshot_path": str(result.snapshot_path.relative_to(self.root)),
            "checksum_path": str(result.checksum_path.relative_to(self.root)),
            "sha256": result.sha256,
            "queue_revision": result.queue_revision,
            "queue_fingerprint": result.queue_fingerprint,
        }, ensure_ascii=False, sort_keys=True))
