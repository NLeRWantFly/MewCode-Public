"""Bounded, typed evidence receipts. Selectors bind a page, not an entire book."""
from __future__ import annotations

import json

from mewcode.file_fingerprints import (
    RAW_FILE_DIGEST_KIND,
    TEXT_DIGEST_KIND,
    content_digest,
    file_digest,
)


def evidence_content(path, *, items_path="", selector=None):
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".docx"}:
        raise ValueError("这是图像/文档，不是文本证据；请用 ReadAuditArtifact 看图，record 使用文本 evidence_ids")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        raise ValueError("文本证据不是 UTF-8；图像请用 ReadAuditArtifact，不要放入文本 evidence") from None
    if selector or items_path:
        value = json.loads(text)
        for key in items_path.split(".") if items_path else []:
            value = value[key]
        if not isinstance(value, list) or not selector:
            raise ValueError("结构化证据需要数组和明确的 selector，不能把整本 JSON 当作单页")
        value = [row for row in value if isinstance(row, dict) and all(row.get(k) == v for k, v in selector.items())]
        if not value:
            raise ValueError("selector 未定位到证据，请核对物理页与来源页索引")
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text


def typed_text_receipt(path, *, relative_path, items_path="", selector=None, text=None):
    """Bind semantic text evidence and exact source bytes without conflating them."""
    selected = text if text is not None else evidence_content(
        path, items_path=items_path, selector=selector
    )
    return {
        "path": relative_path,
        "sha256": content_digest(selected),
        "digest_kind": TEXT_DIGEST_KIND,
        "file_sha256": file_digest(path),
        "file_digest_kind": RAW_FILE_DIGEST_KIND,
        "typed_text": True,
        "items_path": items_path,
        "selector": selector or {},
        'channel': 'text',
    }


def canonical_review_state(state):
    return 'pass' if state == 'complete' else state


def queue_progress(queue):
    counts = {state: 0 for state in ('pending', 'pass', 'needs_repair', 'unknown')}
    for item in queue.get("items", []):
        state = canonical_review_state((queue.get('records', {}).get(item['item_id']) or {}).get('state', 'pending'))
        counts[state if state in counts else 'unknown'] += 1
    return {"total": len(queue.get("items", [])), **counts, 'complete': counts['pass'],
            "reviewed": len(queue.get("records", {})), "revision": queue.get("revision", 0)}


def advance_cursor(progress, previous=None):
    """Finite high-water marks: wording/revision changes don't buy continuations."""
    total, complete = progress.get('total'), progress.get('complete')
    if type(total) is not int or type(complete) is not int or not 0 <= complete <= total <= 10000 or not total:
        return False, previous
    pending = progress.get('pending', total - complete)
    if type(pending) is not int or not 0 <= pending <= total or complete + pending > total:
        return False, previous
    identity = [progress.get('manifest_sha256', ''), total]
    if previous and previous['identity'] != identity:
        return False, previous  # Only an explicit plan migration can change scope.
    prior = previous or {'identity': identity, 'complete': 0, 'pending': total}
    advanced = bool(progress.get('advanced')) and (complete > prior['complete'] or pending < prior['pending'])
    return advanced, {'identity': identity, 'complete': max(prior['complete'], complete),
                      'pending': min(prior['pending'], pending)}
