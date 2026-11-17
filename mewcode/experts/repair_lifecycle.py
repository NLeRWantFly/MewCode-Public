"""Versioned repair inputs: original evidence and derived output are distinct.

No transition here certifies quality. A replacement always needs a new
independent review; predecessor receipts remain immutable in the batch ledger.
"""
from __future__ import annotations

import copy
import hashlib
import json


def item_record_revision(record):
    """Semantic record version; metadata backfills do not revise a decision."""
    body = copy.deepcopy(record)
    if body:
        for receipt in body.get("evidence", []) + body.get("images", []):
            for key in ("digest_kind", "file_sha256", "file_digest_kind"):
                receipt.pop(key, None)
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def reserved_path(reviewer, value):
    """Reserve old output paths even when their bytes were lost or changed."""
    path = (reviewer.root / value).resolve()
    if not path.is_relative_to(reviewer.root):
        raise ValueError("历史产物路径不在工作区内")
    return path


def candidate_artifacts_changed(reviewer, candidate):
    for kind in ("candidate", "diff", "evidence"):
        artifact = candidate[kind]
        reserved_path(reviewer, artifact["path"])
        try:
            if reviewer._digest(artifact["path"]) != artifact["sha256"]:
                return True
        except OSError:
            return True
    return False


def repair_basis(queue, item_id, record, reviewer, owner):
    """Resolve a repairable predecessor without blessing changed originals."""
    candidate = queue.get("repair_candidates", {}).get(item_id)
    if not candidate:
        return copy.deepcopy(record), ""
    predecessor = candidate["candidate_revision"]
    batch = queue.get("repair_batches", {}).get(candidate.get("batch_id"), {})
    if batch.get("owner") != owner or batch.get("status") != "submitted":
        raise ValueError("前一候选不属于当前节点的已提交批次")
    if record.get("state") not in {"needs_repair", "unknown"}:
        raise ValueError("已完成项不能由执行者重新打开")

    # Historical candidate text is review evidence, not an immutable original.
    # Only explicitly registered derived paths can be removed from the basis.
    derived = {reserved_path(reviewer, c[k]["path"]) for b in queue.get("repair_batches", {}).values()
               for key, c in b.get("candidates", {}).items() if key == item_id
               for k in ("candidate", "diff", "evidence")}
    sources = [s for s in candidate.get("sources", []) if reserved_path(reviewer, s["path"]) not in derived]
    evidence = [e for e in candidate.get("source_evidence", []) if reserved_path(reviewer, e["path"]) not in derived]
    if not sources or not evidence:
        raise ValueError("缺少可追溯的原始来源，不能自动换版")
    source_paths = {reviewer._path(s["path"]) for s in sources}
    for source in sources:
        if reviewer._digest(source["path"]) != source["sha256"]:
            raise ValueError("原始来源字节指纹已变化，不能自动换版")
    for receipt in evidence:
        if (reviewer._path(receipt["path"]) not in source_paths
                or reviewer._evidence_digest(receipt) != receipt["sha256"]):
            raise ValueError("原始文本证据已变化，需审查者重新核对来源")
    if (record.get("candidate_revision") != predecessor
            and not candidate_artifacts_changed(reviewer, candidate)):
        raise ValueError("本项已有候选等待独立复核；先交接审查，不能覆盖")
    images = [e for e in record.get("images", []) if reviewer._path(e["path"]) in source_paths]
    basis = {**copy.deepcopy(record), "evidence": copy.deepcopy(evidence), "images": copy.deepcopy(images)}
    basis.pop("evidence_stale", None)
    # Include every original raw source, not only those still in the UI record.
    basis["original_sources"] = copy.deepcopy(sources)
    return basis, predecessor


def needs_new_candidate(queue, item_id):
    """A damaged, previously reviewed output goes back to repair, not reread."""
    record = queue.get("records", {}).get(item_id, {})
    candidate = queue.get("repair_candidates", {}).get(item_id, {})
    return bool(record.get("evidence_stale") and candidate
                and candidate.get("candidate_revision") in {
                    record.get("candidate_revision"), record.get("invalidated_candidate_revision")})


def is_superseded_candidate(queue, item_id, historical_revision, owner):
    """Prove replacement through immutable submitted receipts, not a pointer."""
    batches = queue.get("repair_batches", {})
    history = {c.get("candidate_revision"): c for b in batches.values()
               for key, c in b.get("candidates", {}).items() if key == item_id}
    current = queue.get("repair_candidates", {}).get(item_id, {})
    seen = set()
    while current.get("candidate_revision") not in seen:
        version = current.get("candidate_revision")
        if not version or version == historical_revision:
            return False
        seen.add(version)
        batch = batches.get(current.get("batch_id"), {})
        if (batch.get("owner") != owner or batch.get("status") != "submitted"
                or batch.get("candidates", {}).get(item_id) != current):
            return False
        predecessor = current.get("predecessor_revision")
        if predecessor == historical_revision:
            return True
        current = history.get(predecessor, {})
    return False
