"""Verified, bounded file receipts at successful tool-result boundaries.

Previews are immutable snapshots, never a later read of an arbitrary UI path.
An existing reported file is evidence of existence, not proof of authorship or
acceptance. Failed tools and natural-language claims produce no receipt.
"""
from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_BYTES = 128 * 1024
MAX_RECEIPTS = 200


def capture_process_file(*, tool_name, arguments, is_error, work_dir, sandbox,
                         node_id, tool_id, attempt=1, member_id="", host_observation=None):
    if (is_error or (host_observation or {}).get('reused') is True
            or tool_name not in {"WriteFile", "EditFile", "ReportArtifact"}):
        return None
    raw = str((arguments or {}).get("file_path") or "").strip()
    if not raw or sandbox is None or not sandbox.check(raw, read_only=True)[0]:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(work_dir) / path
    try:
        path = path.resolve(strict=True)
        if not sandbox.check(str(path), read_only=True)[0] or not path.is_file():
            return None
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        truncated_file = len(data) > MAX_FILE_BYTES
        digest = "" if truncated_file else hashlib.sha256(data).hexdigest()
        size = path.stat().st_size
    except (OSError, ValueError):
        return None
    identity = f"{node_id}\0{member_id}\0{attempt}\0{tool_id}\0{path}\0{digest}"
    record = {
        "id": hashlib.sha256(identity.encode()).hexdigest()[:32],
        "node_id": node_id, "member_id": member_id, "node_attempt": attempt,
        "tool_id": tool_id, "name": path.name, "path": str(path),
        "size": size, "sha256": digest, "created_at": time.time(),
        "source": "reported_existing" if tool_name == "ReportArtifact" else "successful_write",
        "accepted": False, "preview_type": "none",
    }
    sample = data[:MAX_PREVIEW_BYTES]
    # Safe raster only; never render SVG, HTML, PDF, or executable content.
    image_type = ("image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else
                  "image/jpeg" if data.startswith(b"\xff\xd8\xff") else "")
    if image_type and len(data) <= MAX_PREVIEW_BYTES:
        record.update(preview_type=image_type, preview=base64.b64encode(data).decode())
    elif b"\x00" not in sample and not image_type:
        try:
            text = sample.decode("utf-8-sig")
        except UnicodeError:
            pass
        else:
            record.update(preview_type="text", preview=text,
                          preview_truncated=size > len(sample))
    return record


def file_receipt(record):
    return {key: value for key, value in record.items() if key != "preview"}


def add_process_file(snapshot, record):
    records = snapshot.setdefault("process_files", [])
    if any(item.get("id") == record["id"] for item in records):
        return
    records.append(record)
    del records[:-MAX_RECEIPTS]
    snapshot["process_file_receipts"] = [file_receipt(item) for item in records]
