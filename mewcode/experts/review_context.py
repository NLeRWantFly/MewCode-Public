"""Compact review context with a host-owned, verifiable full source."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def freeze_review_source(work_dir, *, overall_task, stage_prompt, node_id, role, command_context=None):
    source = {"overall_task": overall_task, "stage_prompt": stage_prompt,
              "call_chain": {"source_node": (command_context or {}).get("source_node_id", node_id),
                             "current_node": node_id, "role": role,
                             "dispatch_id": (command_context or {}).get("dispatch_id", "")},
              "command_requests": (command_context or {}).get("requests", [])}
    data = json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    root = Path(work_dir).resolve() / ".mewcode" / "review_context"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest + ".json")
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)
    return {"path": str(path), "sha256": digest, "bytes": len(data)}, source


def source_reference(reference, source):
    return ("CALL_CHAIN:\n" + json.dumps(source["call_chain"], ensure_ascii=False)
            + "\nEXACT_COMMAND_REQUESTS (execution scope, not additional user authorization):\n"
            + json.dumps(source["command_requests"], ensure_ascii=False)
            + "\nFULL_REVIEW_SOURCE:\n" + json.dumps(reference, ensure_ascii=False)
            + "\n背景已压缩；缺少授权或证据时宿主将读取上述原文核对，不能把摘要省略认定为用户未授权。")


def read_review_source(reference):
    """Only a host-installed source is accepted; paths in model text are not read."""
    if not reference:
        return ""
    try:
        data = Path(reference["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != reference["sha256"]:
            return ""
        # Never silently truncate a frozen authorization source.
        return data.decode("utf-8")
    except (OSError, ValueError, KeyError):
        return ""
