"""Read-only comparison of host-ledger artifacts with current workspace bytes.

Unknown files are never imported as accepted work. A changed digest is a
proposal to revalidate, not a permission to overwrite or certify content.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from mewcode.experts.review_evidence import content_digest, evidence_content, file_digest

POLICY_VERSION = "artifact-reconciliation-v1"


def reconcile_known_artifacts(snapshot: dict[str, Any], root: str | Path,
                              *, max_receipts: int = 1000) -> dict[str, Any]:
    workspace = Path(root).resolve()
    findings: list[dict[str, Any]] = []
    checked = 0
    truncated = False

    def check(path_value: str, expected: str, *, kind: str, item_id: str,
              typed: bool = False, items_path: str = "", selector: Any = None) -> None:
        nonlocal checked, truncated
        if not isinstance(path_value, str) or not isinstance(expected, str):
            return
        if checked >= max_receipts:
            truncated = True
            return
        checked += 1
        opaque = hashlib.sha256(path_value.encode("utf-8")).hexdigest()[:24]
        result = "verified"
        try:
            path = (workspace / path_value).resolve(strict=True)
            if not path.is_relative_to(workspace) or not path.is_file():
                result = "outside_or_not_file"
            elif path.stat().st_size > 64 * 1024 * 1024:
                result = "too_large"
            else:
                actual = (content_digest(evidence_content(path, items_path=items_path, selector=selector))
                          if typed else file_digest(path))
                if actual != expected:
                    result = "digest_changed"
        except FileNotFoundError:
            result = "missing"
        except (OSError, ValueError, TypeError, KeyError):
            result = "unverifiable"
        if result != "verified":
            findings.append({"item_id": item_id, "path_key": opaque,
                             "artifact_kind": kind, "state": result,
                             "action": "revalidate_receipt" if kind != "candidate" else
                                       "independent_review_required"})

    for queue in (snapshot.get("review_queues") or {}).values():
        if not isinstance(queue, dict):
            continue
        check(queue.get("manifest_path"), queue.get("manifest_sha256"),
              kind="manifest", item_id="")
        for item_id, record in (queue.get("records") or {}).items():
            if not isinstance(record, dict):
                continue
            for receipt in (record.get("evidence") or []) + (record.get("images") or []):
                if isinstance(receipt, dict):
                    check(receipt.get("path"), receipt.get("sha256"), kind="review_evidence",
                          item_id=str(item_id), typed=bool(receipt.get("typed_text")),
                          items_path=receipt.get("items_path") or "", selector=receipt.get("selector"))
        for item_id, candidate in (queue.get("repair_candidates") or {}).items():
            if not isinstance(candidate, dict):
                continue
            for key in ("candidate", "diff", "evidence"):
                artifact = candidate.get(key)
                if isinstance(artifact, dict):
                    check(artifact.get("path"), artifact.get("sha256"),
                          kind="candidate", item_id=str(item_id))
            for source in candidate.get("sources") or []:
                if isinstance(source, dict):
                    check(source.get("path"), source.get("sha256"),
                          kind="source", item_id=str(item_id))
    return {"schema_version": 1, "policy_version": POLICY_VERSION,
            "checked_receipts": checked, "truncated": truncated,
            "findings": findings,
            "unknown_files_imported": 0, "business_acceptance_changed": False}
