"""Source-bound receipts for text-only model output continuation.

Every chunk remains in the full conversation transcript. The ledger is only an
index and completion gate; neither a model stop reason nor a hash certifies the
business artifact or independent review.
"""
from __future__ import annotations

import hashlib
from typing import Any


def append_output_chunk(receipts: list[dict[str, Any]], *, event: Any,
                        history: list[Any]) -> dict[str, Any]:
    index = getattr(event, "message_index", None)
    chunk_id = getattr(event, "chunk_id", None)
    digest = getattr(event, "text_sha256", None)
    chars = getattr(event, "chars", None)
    complete = getattr(event, "complete", None)
    if (not isinstance(chunk_id, str) or not chunk_id
            or type(index) is not int or not 0 <= index < len(history)
            or not isinstance(digest, str) or len(digest) != 64
            or type(chars) is not int or chars < 1 or type(complete) is not bool):
        raise ValueError("invalid output chunk receipt")
    source = history[index]
    content = getattr(source, "content", None)
    if (getattr(source, "role", None) != "assistant" or
            getattr(source, "tool_uses", None) or not isinstance(content, str)
            or len(content) != chars
            or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest):
        raise ValueError("output chunk is not bound to an exact assistant message")
    receipt = {"schema_version": 1, "chunk_id": chunk_id,
               "message_index": index,
               "text_sha256": digest, "chars": chars,
               "complete": complete, "business_accepted": False}
    prior = next((row for row in receipts if row.get("chunk_id") == chunk_id), None)
    if prior is not None:
        if {key: prior.get(key) for key in receipt} != receipt:
            raise ValueError("conflicting output chunk receipt")
        return prior
    # Conversation compaction may renumber message indices. The host-owned
    # chunk ID, not the transient transcript index, is the replay key.
    receipt["ordinal"] = len(receipts) + 1
    receipts.append(receipt)
    return receipt
