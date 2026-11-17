"""Public, source-addressed history for planning; independent of compacted context."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .repair_protocol import CONTROL_PURPOSE, is_repair_protocol

MAX_HISTORY_BYTES = 100 * 1024 * 1024
# This bound applies to each scan segment, each record and all retained source
# evidence. Larger raw logs are composed from complete bounded segments; no
# original records are archived, truncated or silently skipped to fit the cap.
SOURCE_CHARS = 6000
CHUNK_CHARS = 18000
HUMAN_TOOLS = {"human_oncall", "AskUserQuestion", "ask_user", "ask_user_question"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def public_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def read_sources(path: Path):
    """Read a fixed complete-line snapshot, not SessionManager.resume's summary."""
    if not path.is_file():
        return [], ["找不到此会话的原始历史；不能声称已补全。"], {}
    from .history_scan import history_lines
    sources, warnings, human_calls, answered = [], [], {}, set()
    line_no = compact_count = retained_bytes = 0
    scan = {}

    def retain(value):
        nonlocal retained_bytes
        retained_bytes += len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if retained_bytes > MAX_HISTORY_BYTES:
            raise ValueError("历史来源整理结果超过安全读取上限；未丢弃早期要求或提高上限，不能生成完整来源快照。")

    def warn(text):
        retain(text)
        warnings.append(text)

    def add(role, text, record, kind="message", attachments=None):
        text = text.strip()
        if not text or text.startswith(("<system-reminder>", "<environment_context>",
                                         "<send_user_message_question_reply>")):
            return
        control = is_repair_protocol(text, role)
        for start in range(0, len(text), SOURCE_CHARS):
            piece = text[start:start + SOURCE_CHARS]
            item = {"role": role, "kind": kind, "text": piece, "line": line_no,
                    "part": start // SOURCE_CHARS + 1, "parts": (len(text) + SOURCE_CHARS - 1) // SOURCE_CHARS,
                    "at": str(record.get("timestamp") or ""), "attachments": attachments or []}
            item["id"] = "s_" + digest(item)[:24]
            # Classification is outside the fingerprint: old source IDs and
            # exact original text survive this index-only upgrade.
            if control:
                item["purpose"] = CONTROL_PURPOSE
            retain(item)
            sources.append(item)

    for raw in history_lines(path, limit=MAX_HISTORY_BYTES, scan=scan):
        line_no += 1
        if not raw.endswith(b"\n"):
            warn(f"第 {line_no} 行尚未完整落盘，留待下次处理。")
            continue
        try:
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            warn(f"第 {line_no} 行无法解析，未静默跳过。")
            continue
        kind, content = record.get("type"), record.get("content")
        if kind == "compact_boundary":
            compact_count += 1
            continue
        if kind in {"user", "assistant"}:
            attachments = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and block.get("name") in HUMAN_TOOLS:
                        args = block.get("input") or {}
                        if isinstance(args, dict):
                            question = {
                                key: args[key] for key in ("question", "questions", "context", "options") if key in args}
                            retain([block.get("id"), question])
                            human_calls[block.get("id")] = question
                    if block.get("type") in {"image", "document", "file"}:
                        attachments.append({key: str(block[key])[:500]
                                            for key in ("id", "name", "mime_type", "attachment_id") if key in block})
            text = public_text(content)
            if attachments:
                text += "\n附件引用（未读取附件正文）：" + json.dumps(attachments, ensure_ascii=False)
            add(kind, text, record, attachments=attachments)
        elif kind == "tool_result" and record.get("tool_use_id") in human_calls:
            call_id = record["tool_use_id"]
            if call_id in answered or record.get("is_error"):
                continue
            try:
                answer = json.loads(content) if isinstance(content, str) else content
            except ValueError:
                continue
            if not isinstance(answer, dict) or answer.get("status") != "answered":
                continue
            answered.add(call_id)
            # No tool output is treated as an instruction except a paired,
            # successfully answered human-interaction result.
            allowed = {key: answer[key] for key in ("choice", "answer", "answers", "text") if key in answer}
            add("user", "人工问答\n问题：" + json.dumps(human_calls[call_id], ensure_ascii=False)
                + "\n回答：" + json.dumps(allowed, ensure_ascii=False), record, "human_answer")
    return sources, warnings, {**scan, "lines": line_no, "compactions": compact_count,
                                "source_count": len(sources), "snapshot": digest([s["id"] for s in sources])}


def advance_cursor(sources, processed_ids, completed_ids=(), *, reused_ids=()):
    """Advance only through persisted results plus this successfully applied block.

    Selected replies may already be materialized out of order. They can bridge
    gaps, but neither a missing source nor an unfinished block can move the
    contiguous history cursor. Source IDs bind the original content/position.
    """
    ids = [s["id"] for s in sources]
    reused, completed = set(reused_ids), set(completed_ids)
    if ids[:len(processed_ids)] != list(processed_ids):
        raise ValueError("已整理的原始历史发生变化或缺失；请保留已确认版本并重新扫描，不能续接错误游标。")
    if not reused.issubset(ids):
        raise ValueError("已生成图的来源发生变化或缺失，不能复用旧进度；请核对来源后重新扫描。")
    cursor = list(processed_ids)
    for id in ids[len(cursor):]:
        if id not in reused and id not in completed:
            break
        cursor.append(id)
    if not completed.issubset(cursor):
        raise ValueError("本批完成记录跳过了未处理来源，未推进进度。")
    return cursor


def next_chunk(sources, processed_ids, *, source_limit=None, reused_ids=()):
    if source_limit is not None and (type(source_limit) is not int or source_limit < 1):
        raise ValueError("底稿分块数量上限必须为正整数")
    advance_cursor(sources, processed_ids, reused_ids=reused_ids)
    reused = set(reused_ids)
    start, total, batch = len(processed_ids), 0, []
    for source in sources[start:]:
        if source["id"] in reused:
            continue
        cost = len(json.dumps(source, ensure_ascii=False))
        if batch and (total + cost > CHUNK_CHARS or
                      source_limit is not None and len(batch) >= source_limit):
            break
        batch.append(source)
        total += cost
    return batch


def select_messages(sources, selection):
    """Resolve displayed public text against persisted messages, never trust it as evidence.

    Streaming replies can contain several assistant messages separated by tools.
    Ambiguous repeats are rejected rather than silently selecting the latest one.
    """
    texts = selection.get("texts") if isinstance(selection, dict) else None
    if (not isinstance(texts, list) or not 1 <= len(texts) <= 100
            or any(not isinstance(t, str) or not t.strip() for t in texts)
            or sum(len(t) for t in texts) > 160000):
        raise ValueError("请选择一段有效的助手回复，过长的回复请分段纳入")
    normalize = lambda value: value.replace("\r\n", "\n").strip()
    texts = [normalize(t) for t in texts]
    groups = []
    for source in sources:
        if not groups or groups[-1][0]["line"] != source["line"]:
            groups.append([])
        groups[-1].append(source)
    matches = []
    for start in range(len(groups) - len(texts) + 1):
        selected = groups[start:start + len(texts)]
        if all(g[0]["role"] == "assistant" and g[0]["kind"] == "message"
               and normalize("".join(s["text"] for s in g)) == text for g, text in zip(selected, texts)):
            matches.append(start)
    if len(matches) != 1:
        raise ValueError("这段回复有多个相同的历史来源，无法唯一定位；请选取更具体的回复" if matches else
                         "这段回复尚未完整落盘或不是原始对话消息；请待回复结束并刷新后重试")
    start = matches[0]
    selected = [s for group in groups[start:start + len(texts)] for s in group]
    # Reference-only context: at most the preceding three messages / 18k chars.
    # Never mark it processed, or use it as permission to change other requirements.
    nearby, size = [], 0
    for group in reversed(groups[max(0, start - 3):start]):
        cost = sum(len(s["text"]) for s in group)
        if size + cost > CHUNK_CHARS:
            break
        nearby = group + nearby
        size += cost
        if group[0]["role"] == "user":
            break
    return selected, nearby


def estimate_chunks(sources, processed_ids, *, source_limit=None, reused_ids=()):
    """Remaining chunk count under the same CHUNK_CHARS rule as next_chunk.

    Returns 0 when the processed cursor no longer matches the source prefix,
    instead of raising like next_chunk does.
    """
    if source_limit is not None and (type(source_limit) is not int or source_limit < 1):
        raise ValueError("底稿分块数量上限必须为正整数")
    try:
        advance_cursor(sources, processed_ids, reused_ids=reused_ids)
    except ValueError:
        return 0
    reused = set(reused_ids)
    remaining = [s for s in sources[len(processed_ids):] if s["id"] not in reused]
    count, total, items = 0, 0, 0
    for source in remaining:
        cost = len(json.dumps(source, ensure_ascii=False))
        if total and (total + cost > CHUNK_CHARS or
                      source_limit is not None and items >= source_limit):
            count += 1
            total = 0
            items = 0
        total += cost
        items += 1
    return count + (1 if remaining else 0)
