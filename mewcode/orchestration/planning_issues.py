"""Narrow preparation-question triage for the workflow planning surface."""
from __future__ import annotations

import copy
import re


PREPARATION_QUESTION_GUIDANCE = """\
Planning questions about service wiring and runtime inputs are preparation work, not business choices.
If GLM-OCR is absent from available_model_routes, treat that list as model-route inventory only: it does not establish whether the separate OCR tool service is installed. Keep the issue as an unresolved, nonblocking capability question until the host configuration is checked; do not claim OCR is ready or ask the user to choose a substitute.
If physical-page ranges or foreach.items are missing, keep them as technical/runtime-input preparation. Do not invent page IDs, item batches, or a successful readiness result. Ask the user only when their business preference or private input is actually needed.
Only apply this guidance to source-backed AI-authored questions with no options. Preserve user-authored, manually reviewed, and option-bearing questions. Leave unrelated questions unclassified for the normal classification flow.
"""

_PROTECTED_EVIDENCE = {"user_source", "saved_graph", "manual"}
_OCR_MARKER = re.compile(r"\bglm[\s_-]*ocr\b|智谱.{0,8}ocr|ocr", re.IGNORECASE)
_ROUTE_ABSENCE = re.compile(
    r"(?:not\s+(?:in|listed)|absent|missing|not\s+found|未|不在|没有|缺少|缺失|找不到|未找到|未列出|不存在)"
    r".{0,35}(?:available_model_routes|model routes|模型.{0,8}路由|模型列表)|"
    r"(?:available_model_routes|model routes|模型.{0,8}路由|模型列表)"
    r".{0,35}(?:not\s+(?:in|listed)|absent|missing|not\s+found|未|不在|没有|缺少|缺失|找不到|未找到|未列出|不存在)",
    re.IGNORECASE,
)
_SERVICE_GAP = re.compile(
    r"(?:缺少|缺失|未配置|未接入|不可用|不存在|找不到|未找到|not\s+configured|unavailable|missing)"
    r".{0,20}(?:ocr(?:\s+tool|\s+service)?|工具服务|服务配置)|"
    r"(?:ocr(?:\s+tool|\s+service)?|工具服务|服务配置)"
    r".{0,20}(?:缺少|缺失|未配置|未接入|不可用|不存在|找不到|未找到|not\s+configured|unavailable|missing)",
    re.IGNORECASE,
)
_PAGE_INPUT = re.compile(
    r"physical[ -]pages?|page(?:s| numbers?| ranges?| ids?)|页码|物理页|纸页|页批次|页范围",
    re.IGNORECASE,
)
_FOREACH_ITEMS = re.compile(r"foreach\s*[.]\s*items|foreach|item_id", re.IGNORECASE)


def _source_quotes(entry: dict) -> list[str]:
    refs = entry.get("sources")
    if not isinstance(refs, list):
        return []
    return [ref["quote"] for ref in refs
            if isinstance(ref, dict) and isinstance(ref.get("quote"), str) and ref["quote"].strip()]


def _protected(entry: dict) -> bool:
    if entry.get("reviewed") is True or entry.get("evidence") in _PROTECTED_EVIDENCE:
        return True
    refs = entry.get("sources")
    if isinstance(refs, list):
        return any(isinstance(ref, dict) and (
            ref.get("role") == "user" or ref.get("evidence") in _PROTECTED_EVIDENCE
        ) for ref in refs)
    return False


def _ai_authored(entry: dict) -> bool:
    if entry.get("evidence") in {"proposal", "ai_completed"}:
        return True
    refs = entry.get("sources")
    return isinstance(refs, list) and any(
        isinstance(ref, dict) and (ref.get("role") == "assistant" or ref.get("kind") == "workflow_material")
        for ref in refs
    )


def _has_no_options(data: dict) -> bool:
    # Any supplied non-empty value may encode a real user choice. Malformed
    # options stay visible for the ordinary validator instead of being erased.
    return not data.get("options")


def _ocr_preparation(entry: dict, quotes: list[str]) -> bool:
    if not quotes or not any(_OCR_MARKER.search(quote) for quote in quotes):
        return False
    content = " ".join([entry.get("title", ""), entry.get("body", ""), *quotes])
    return bool(_OCR_MARKER.search(content)
                and (_ROUTE_ABSENCE.search(content) or _SERVICE_GAP.search(content)))


def _batch_preparation(entry: dict, quotes: list[str]) -> str | None:
    if not quotes:
        return None
    source_text = " ".join(quotes)
    content = " ".join([entry.get("title", ""), entry.get("body", ""), source_text])
    page_cue = bool(_PAGE_INPUT.search(content))
    foreach_cue = bool(_FOREACH_ITEMS.search(content))
    if not (page_cue or foreach_cue):
        return None
    # Concrete physical page ranges are user runtime input. A bare foreach
    # schema gap is technical preparation, while either remains nonblocking.
    return "input" if page_cue else "technical"


def triage_preparation_questions(document: dict) -> dict:
    """Copy a workflow document and relabel only source-backed AI prep issues.

    OCR service wiring remains an unresolved, nonblocking capability question.
    Missing page/item batches remain input or technical preparation; this
    helper never creates items, resolves questions, or changes user evidence.
    """
    result = copy.deepcopy(document)
    entries = result.get("entries", [])
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "question" or entry.get("state", "active") != "active":
            continue
        if _protected(entry) or not _ai_authored(entry):
            continue
        data = entry.get("data")
        if not isinstance(data, dict) or not _has_no_options(data):
            continue
        quotes = _source_quotes(entry)
        if _ocr_preparation(entry, quotes):
            data["issue_type"] = "capability"
            data["blocking"] = False
            continue
        preparation_type = _batch_preparation(entry, quotes)
        if preparation_type:
            data["issue_type"] = preparation_type
            data["blocking"] = False
    return result


def deferred_capability_question_ids(document: dict) -> set[str]:
    """Identify unresolved AI capability checks explicitly owned by a work stage.

    These may remain visible in a saved design. They are not capability receipts,
    resolved questions, or permission to execute that design.
    """
    entries = document.get("entries", [])
    owners = {entry["id"] for entry in entries
              if entry.get("kind") == "node" and entry.get("state") == "active"
              and (data := entry.get("data") or {}).get("node_kind") == "implement"
              and data.get("steps") and data.get("acceptance")}
    return {entry["id"] for entry in entries
            if entry.get("kind") == "question" and entry.get("state") == "active"
            and not _protected(entry) and _ai_authored(entry) and _source_quotes(entry)
            and (data := entry.get("data") or {}).get("issue_type") == "capability"
            and data.get("blocking") is False and _has_no_options(data)
            and data.get("node_id") in owners}


def pending_question_gate(document: dict, *, plan_only: bool = False) -> tuple[str, list[str]]:
    """Re-evaluate unresolved questions against the caller's current intent."""
    from .run_inputs import resolved_question_ids
    resolved = resolved_question_ids(document)
    deferred = deferred_capability_question_ids(document) if plan_only is True else set()
    questions = [entry for entry in document.get("entries", [])
                 if entry.get("kind") == "question" and entry.get("state") == "active"
                 and entry["id"] not in resolved and entry["id"] not in deferred]
    reason = ("blocking_goal" if any((entry.get("data") or {}).get("blocking") is True
                                      for entry in questions) else "pending_questions") if questions else ""
    return reason, sorted(deferred)
