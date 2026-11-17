"""Prepare safe, reusable choices for orchestration interactions.

The orchestration layer has a few different kinds of questions.  Some of
them are closed enums, while others need candidates discovered from the
current project.  This module keeps the discovery protocol deliberately
small: the model proposes read-only glob searches, the host performs those
searches and returns metadata, and the model turns that metadata into
choices.  No command is executed and no file contents are sent to the
model.

``prepare_choices`` returns a serialisable bundle.  ``project_questions``
projects that bundle into the existing ``options`` contract, and
``resolve_answers`` turns choice ids back into actual values or explicit
actions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Awaitable, Callable, Iterable


Complete = Callable[[str, str], Awaitable[str]]

_ACTIONS = frozenset(
    {"research", "revise", "configure", "retry_prepare", "defer", "manual"}
)
_ACTION_LABELS = {
    "research": "研究或补充材料",
    "revise": "修改问题或约束",
    "configure": "配置处理方式",
    "retry_prepare": "重新准备选项",
    "defer": "稍后处理",
    "manual": "其他",
}
_ACTION_INPUT_LABELS = {
    "research": "目录或材料线索",
    "revise": "修改说明",
    "configure": "配置说明",
    "retry_prepare": "补充说明（可选）",
    "manual": "其他填写",
}

_TOP_KEYS = frozenset({"searches", "questions"})
_SEARCH_KEYS = frozenset({"root", "pattern"})
_QUESTION_KEYS = frozenset({"name", "question", "description", "input_mode", "choices"})
_CHOICE_KEYS = frozenset(
    {
        "id",
        "label",
        "description",
        "recommended",
        "kind",
        "value",
        "action",
        "requires_input",
        "input_label",
        "evidence",
        "verification",
    }
)

_PATH_SUFFIXES = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".md",
        ".txt",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".svg",
        ".zip",
    }
)
_SENSITIVE_PARTS = frozenset(
    {
        ".env",
        ".pem",
        ".key",
        "secret",
        "credential",
        "password",
        "passwd",
        "token",
        "id_rsa",
    }
)


class _InvalidModelOutput(ValueError):
    """A model response that should be repaired before it is accepted."""


def _json_safe(value: Any) -> Any:
    """Return a deterministic, non-executable representation for hashing/prompts."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    # The public API is JSON-shaped.  Keeping a stable representation here is
    # safer than invoking arbitrary ``__str__`` implementations in prompts.
    return repr(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _text(value: Any, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _InvalidModelOutput(f"{field} 必须是文本")
    if "\x00" in value or value != value.strip():
        raise _InvalidModelOutput(f"{field} 含有无效空白或控制字符")
    if not allow_empty and not value:
        raise _InvalidModelOutput(f"{field} 不能为空")
    return value


def _normalise_roots(allowed_roots: Iterable[str]) -> list[Path]:
    if isinstance(allowed_roots, (str, bytes)):
        raise ValueError("allowed_roots 必须是目录列表")
    roots: list[Path] = []
    for raw in allowed_roots:
        if not isinstance(raw, (str, os.PathLike)):
            raise ValueError("allowed_roots 只能包含路径")
        path = Path(raw).expanduser().resolve(strict=False)
        if not path.exists() or not path.is_dir():
            # A missing root cannot produce verified files.  It is retained in
            # the scope hash so a later creation cannot reuse an old bundle.
            if path not in roots:
                roots.append(path)
        elif path not in roots:
            roots.append(path)
    return sorted(roots, key=lambda item: os.path.normcase(str(item)))


def _root_signatures(roots: Iterable[Path]) -> list[dict[str, Any]]:
    signatures: list[dict[str, Any]] = []
    for root in roots:
        try:
            root.stat()
        except OSError:
            signatures.append({"path": str(root), "exists": False})
            continue
        signatures.append(
            {
                "path": str(root),
                "exists": True,
                "is_dir": root.is_dir(),
            }
        )
    return signatures


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.resolve(strict=False)
    for root in roots:
        try:
            if resolved.is_relative_to(root):
                return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


def _question_contract(questions: list[dict[str, Any]]) -> Any:
    # prepared_choices is generated output and must not change the input
    # contract used for cache validation.
    return [
        {key: _json_safe(value) for key, value in sorted(question.items()) if key != "prepared_choices"}
        for question in questions
    ]


def _context_hash(
    identity: dict[str, Any],
    questions: list[dict[str, Any]],
    context: dict[str, Any],
    roots: list[Path],
) -> str:
    return _sha256(
        {
            "protocol_version": 3,
            "identity": identity,
            "questions": _question_contract(questions),
            "context": context,
            "allowed_roots": [str(root) for root in roots],
            "root_signatures": _root_signatures(roots),
        }
    )


def _parse_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        raise _InvalidModelOutput("模型输出必须是 JSON 文本")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # A short explanatory prefix is common in model output.  Extracting
        # only the outer JSON object/array does not execute or evaluate it.
        starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
        ends = [index for index in (text.rfind("}"), text.rfind("]")) if index >= 0]
        if not starts or not ends:
            raise _InvalidModelOutput("模型输出不是有效 JSON")
        candidate = text[min(starts) : max(ends) + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise _InvalidModelOutput("模型输出不是有效 JSON") from exc


def _unknown_keys(value: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _InvalidModelOutput(f"{where} 含有未声明字段: {', '.join(unknown)}；请修正对应项")


def _validate_searches(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _InvalidModelOutput("searches 必须是列表")
    searches: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _InvalidModelOutput(f"searches[{index}] 必须是对象")
        _unknown_keys(item, _SEARCH_KEYS, f"searches[{index}]")
        root = _text(item.get("root"), field=f"searches[{index}].root")
        pattern = _text(item.get("pattern"), field=f"searches[{index}].pattern")
        searches.append({"root": root, "pattern": pattern})
    return searches


def _validate_question_shell(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _InvalidModelOutput("questions 必须是列表")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _InvalidModelOutput(f"questions[{index}] 必须是对象")
        _unknown_keys(item, _QUESTION_KEYS, f"questions[{index}]")
        name = _text(item.get("name"), field=f"questions[{index}].name")
        choices = item.get("choices", [])
        if not isinstance(choices, list):
            raise _InvalidModelOutput(f"questions[{index}].choices 必须是列表")
        row = {"name": name, "choices": choices}
        for field in ("question", "description"):
            if field in item:
                row[field] = _text(item[field], field=f"questions[{index}].{field}", allow_empty=field == "description")
                if len(row[field]) > 2000:
                    raise _InvalidModelOutput(f"questions[{index}].{field} 超过 2000 字")
        if "input_mode" in item:
            if not isinstance(item["input_mode"], str) or item["input_mode"] not in {"choice", "text"}:
                raise _InvalidModelOutput(f"questions[{index}].input_mode 必须是 choice 或 text")
            row["input_mode"] = item["input_mode"]
        result.append(row)
    return result


def _validate_discovery(value: Any) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise _InvalidModelOutput("发现结果必须是 JSON 对象")
    _unknown_keys(value, _TOP_KEYS, "发现结果")
    return _validate_searches(value.get("searches", [])), _validate_question_shell(value.get("questions", []))


def _validate_choice_shape(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _InvalidModelOutput(f"{where} 必须是对象")
    _unknown_keys(value, _CHOICE_KEYS, where)
    if "label" in value:
        label = _text(value["label"], field=f"{where}.label")
        if len(label) > 100:
            raise _InvalidModelOutput(f"{where}.label 超过 100 字")
    if "description" in value:
        description = _text(value["description"], field=f"{where}.description", allow_empty=True)
        if len(description) > 300:
            raise _InvalidModelOutput(f"{where}.description 超过 300 字")
    if "id" in value:
        _text(value["id"], field=f"{where}.id")
    if "recommended" in value and type(value["recommended"]) is not bool:
        raise _InvalidModelOutput(f"{where}.recommended 必须是布尔值")
    if "requires_input" in value and type(value["requires_input"]) is not bool:
        raise _InvalidModelOutput(f"{where}.requires_input 必须是布尔值")
    if "evidence" in value:
        if not isinstance(value["evidence"], list) or any(not isinstance(item, str) for item in value["evidence"]):
            raise _InvalidModelOutput(f"{where}.evidence 必须是文本列表")
    if "verification" in value and value["verification"] not in {"verified", "suggested"}:
        raise _InvalidModelOutput(f"{where}.verification 无效")
    if "kind" in value and value["kind"] not in {"value", "action"}:
        raise _InvalidModelOutput(f"{where}.kind 无效")
    if "action" in value and value["action"] not in _ACTIONS:
        raise _InvalidModelOutput(f"{where}.action 不受支持")
    if "input_label" in value:
        _text(value["input_label"], field=f"{where}.input_label")
    if "value" in value:
        candidate = value["value"]
        if not isinstance(candidate, (str, list)):
            raise _InvalidModelOutput(f"{where}.value 必须是文本或文本列表")
        if isinstance(candidate, str):
            candidate_text = _text(candidate, field=f"{where}.value")
            if len(candidate_text) > 500:
                raise _InvalidModelOutput(f"{where}.value 超过 500 字")
        elif not candidate or any(
            not isinstance(item, str) or not item.strip() or item != item.strip() for item in candidate
        ):
            raise _InvalidModelOutput(f"{where}.value 必须是非空文本列表")
        elif any(len(item) > 500 for item in candidate):
            raise _InvalidModelOutput(f"{where}.value 列表含有超过 500 字的值")
    kind = value.get("kind") or ("action" if value.get("action") else "value")
    if kind == "action" and value.get("action") not in _ACTIONS:
        raise _InvalidModelOutput(f"{where}.action 不受支持")
    if kind == "value" and "value" not in value:
        raise _InvalidModelOutput(f"{where}.value 缺失")
    return value


def _validate_choices(value: Any) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise _InvalidModelOutput("选项结果必须是 JSON 对象")
    _unknown_keys(value, _TOP_KEYS, "选项结果")
    searches = _validate_searches(value.get("searches", []))
    questions = _validate_question_shell(value.get("questions", []))
    for q_index, question in enumerate(questions):
        for c_index, choice in enumerate(question["choices"]):
            _validate_choice_shape(choice, f"questions[{q_index}].choices[{c_index}]")
    return searches, questions


async def _model_json(
    complete: Complete,
    *,
    system: str,
    prompt: str,
    validator: Callable[[Any], Any],
    phase: str,
    attempts: int = 3,
) -> tuple[Any, str | None]:
    last_error: str | None = None
    current_prompt = prompt
    for attempt in range(attempts):
        try:
            raw = await complete(system, current_prompt)
            parsed = _parse_json(raw)
            value = validator(parsed)
            return value, None
        except Exception as exc:  # callback and validation failures are user-visible bundle errors
            last_error = str(exc) or exc.__class__.__name__
            if attempt + 1 >= attempts:
                break
            current_prompt = (
                f"上一轮 {phase} 输出无法接受。请只修正对应项并重新输出完整 JSON。\n"
                f"错误：{last_error}\n"
                "约束：只能返回 JSON；不得增加未声明字段；不得调用命令；"
                "只能使用允许的动作和只读路径搜索。\n"
                f"原始输出：{_safe_prompt(raw if 'raw' in locals() else '')}"
            )
    return None, last_error or f"{phase} 失败"


def _safe_prompt(value: Any) -> str:
    text = value if isinstance(value, str) else _canonical_json(value)
    return text


def _safe_search_pattern(pattern: str) -> bool:
    normal = pattern.replace("\\", "/").lower()
    if any(part in normal for part in _SENSITIVE_PARTS):
        return False
    # An empty/broad glob would amount to an unbounded scan.  Specific
    # recursive patterns such as **/*.pdf remain useful and bounded by the
    # allowed root and the result cap.
    if normal in {"", ".", "*", "**", "**/*", "*.*"}:
        return False
    if "\x00" in pattern:
        return False
    return True


def _safe_relative_pattern(pattern: str) -> str | None:
    if not _safe_search_pattern(pattern):
        return None
    if PurePosixPath(pattern).is_absolute() or PureWindowsPath(pattern).is_absolute():
        return None
    normal = pattern.replace("\\", "/")
    if any(part == ".." for part in PurePosixPath(normal).parts):
        return None
    return normal


def _resolve_search_root(raw: str, roots: list[Path]) -> Path | None:
    candidate = Path(raw).expanduser().resolve(strict=False)
    if not _inside(candidate, roots):
        return None
    try:
        if not candidate.exists() or not candidate.is_dir():
            return None
    except OSError:
        return None
    return candidate


def _file_metadata(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {}
    return {
        "path": str(path.resolve(strict=False)),
        "name": path.name,
        "suffix": path.suffix.lower(),
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _discover_files(searches: list[dict[str, str]], roots: list[Path]) -> tuple[list[dict[str, Any]], list[str]]:
    files: dict[str, dict[str, Any]] = {}
    rejected: list[str] = []
    for search in searches:
        root = _resolve_search_root(search["root"], roots)
        pattern = _safe_relative_pattern(search["pattern"])
        if root is None or pattern is None:
            rejected.append(f"拒绝搜索：{search.get('root')} / {search.get('pattern')}")
            continue
        try:
            matches = root.glob(pattern)
        except (OSError, ValueError) as exc:
            rejected.append(f"搜索失败：{search.get('pattern')} ({exc})")
            continue
        count = 0
        for path in matches:
            if count >= 512:
                rejected.append(f"搜索结果过多，已截断：{search.get('pattern')}")
                break
            try:
                resolved = path.resolve(strict=False)
                if not _inside(resolved, roots) or not (path.is_file() or path.is_dir()):
                    continue
                metadata = _file_metadata(path)
            except (OSError, RuntimeError, ValueError):
                continue
            if metadata:
                files[metadata["path"]] = metadata
                count += 1
    return sorted(files.values(), key=lambda item: item["path"]), rejected


def _looks_like_path(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
        return True
    normal = value.replace("\\", "/")
    if normal.startswith(("./", "../", "/")) or "/" in normal:
        return True
    return Path(value).suffix.lower() in _PATH_SUFFIXES


def _candidate_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return []


def _path_for_verification(raw: str, roots: list[Path]) -> Path | None:
    normal = raw.replace("\\", "/").lower()
    if any(part in normal for part in _SENSITIVE_PARTS):
        return None
    path = Path(raw).expanduser()
    if path.is_absolute() or PureWindowsPath(raw).is_absolute():
        resolved = path.resolve(strict=False)
        return resolved if _inside(resolved, roots) else None
    for root in roots:
        candidate = (root / path).resolve(strict=False)
        if _inside(candidate, roots) and candidate.exists():
            return candidate
    return None


def _verify_candidate_paths(
    value: Any,
    evidence: list[str],
    roots: list[Path],
) -> tuple[str, list[str]]:
    strings = _candidate_strings(value)
    path_items = [item for item in strings if _looks_like_path(item)]
    if not path_items:
        # Textual options may carry a model's verification state.  They do not
        # claim that a path was observed, so no filesystem check is needed.
        return "suggested", list(evidence)

    verified_paths: list[str] = []
    all_verified = len(path_items) == len(strings) and bool(strings)
    for item in strings:
        if not _looks_like_path(item):
            all_verified = False
            continue
        resolved = _path_for_verification(item, roots)
        if resolved is None or not resolved.exists():
            all_verified = False
        else:
            verified_paths.append(str(resolved))

    clean_evidence: list[str] = []
    for ref in evidence:
        if not isinstance(ref, str):
            continue
        if _looks_like_path(ref):
            resolved = _path_for_verification(ref, roots)
            if resolved is None or not resolved.exists():
                all_verified = False
            else:
                clean_evidence.append(str(resolved))
        else:
            clean_evidence.append(ref)
    if not clean_evidence:
        clean_evidence = verified_paths
    return ("verified" if all_verified else "suggested"), _dedupe_strings(clean_evidence)


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _slug(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return result[:64] or "choice"


def _choice_id(name: str, choice: dict[str, Any], index: int) -> str:
    if isinstance(choice.get("id"), str) and choice["id"].strip():
        return choice["id"]
    if choice.get("kind") == "action" or choice.get("action"):
        action = choice.get("action") or "action"
        return f"{_slug(name)}-{action}"
    digest = _sha256(
        {
            "name": name,
            "label": choice.get("label", ""),
            "value": choice.get("value"),
            "index": index,
        }
    )[:10]
    return f"{_slug(name)}-{digest}"


def _normalise_action(
    name: str,
    choice: dict[str, Any],
    *,
    choice_id: str,
) -> dict[str, Any]:
    action = choice.get("action")
    if action not in _ACTIONS:
        raise _InvalidModelOutput(f"问题 {name} 的动作不受支持")
    requires = choice.get("requires_input", action in {"research", "revise", "configure", "manual"})
    if type(requires) is not bool:
        raise _InvalidModelOutput(f"问题 {name} 的 requires_input 无效")
    result = {
        "id": choice_id,
        "label": choice.get("label") or _ACTION_LABELS[action],
        "description": choice.get("description") or "",
        "recommended": bool(choice.get("recommended", False)),
        "kind": "action",
        "action": action,
        "requires_input": requires,
        "evidence": _dedupe_strings(choice.get("evidence") or []),
        "verification": "suggested",
    }
    if requires:
        result["input_label"] = choice.get("input_label") or _ACTION_INPUT_LABELS.get(action, "补充说明")
    return result


def _normalise_value(
    name: str,
    choice: dict[str, Any],
    *,
    choice_id: str,
    roots: list[Path],
) -> dict[str, Any]:
    if "value" not in choice:
        raise _InvalidModelOutput(f"问题 {name} 的值选项缺少 value")
    value = choice["value"]
    if not isinstance(value, (str, list)):
        raise _InvalidModelOutput(f"问题 {name} 的 value 必须是文本或文本列表")
    if isinstance(value, str):
        if not value or value != value.strip() or "\x00" in value:
            raise _InvalidModelOutput(f"问题 {name} 的 value 无效")
    elif any(not isinstance(item, str) or not item or item != item.strip() or "\x00" in item for item in value):
        raise _InvalidModelOutput(f"问题 {name} 的 value 列表无效")
    evidence = choice.get("evidence") or []
    verification, evidence = _verify_candidate_paths(value, evidence, roots)
    result = {
        "id": choice_id,
        "label": choice.get("label") or str(value),
        "description": choice.get("description") or "",
        "recommended": bool(choice.get("recommended", False)),
        "kind": "value",
        "value": _clone(value),
        "requires_input": bool(choice.get("requires_input", False)),
        "evidence": evidence,
        "verification": verification,
    }
    if result["requires_input"]:
        result["input_label"] = choice.get("input_label") or "补充说明"
    return result


def _original_options(question: dict[str, Any]) -> list[dict[str, Any]]:
    options = question.get("options")
    if not isinstance(options, list):
        return []
    result = []
    for option in options:
        if not isinstance(option, dict) or "value" not in option or "label" not in option:
            continue
        value = option["value"]
        if not isinstance(value, (str, list)):
            continue
        result.append(
            {
                "id": "",
                "label": option.get("label", ""),
                "description": option.get("description", ""),
                "recommended": bool(option.get("recommended", False)),
                "kind": "value",
                "value": _clone(value),
                "evidence": [],
                "verification": "suggested",
            }
        )
    return result


def _context_defaults(context: dict[str, Any], name: str) -> list[Any]:
    result: list[Any] = []
    for key in (
        "defaults",
        "default_values",
        "graph_defaults",
        "existing_values",
        "current_values",
        "existing_defaults",
        "values",
        "parameters",
    ):
        source = context.get(key) if isinstance(context, dict) else None
        if not isinstance(source, dict) or name not in source:
            continue
        value = source[name]
        if isinstance(value, dict):
            value = value.get("value", value.get("default"))
        if isinstance(value, list):
            # A list is one actual parameter value (for example a set of four
            # PDFs), rather than four unrelated default choices.
            result.append(_clone(value))
        elif isinstance(value, (str, int, float, bool)):
            result.append(_clone(value))
    return result


def _choice_key(choice: dict[str, Any]) -> str:
    if choice.get("kind") == "action":
        return f"action:{choice.get('action')}"
    return f"value:{_canonical_json(choice.get('value'))}"


def _append_choice(choices: list[dict[str, Any]], choice: dict[str, Any]) -> None:
    key = _choice_key(choice)
    if any(_choice_key(existing) == key for existing in choices):
        return
    existing_ids = {existing.get("id") for existing in choices}
    if choice.get("id") in existing_ids:
        base = str(choice.get("id") or "choice")
        suffix = 2
        while f"{base}-{suffix}" in existing_ids:
            suffix += 1
        choice["id"] = f"{base}-{suffix}"
    choices.append(choice)


def _action_choice(action: str, *, requires_input: bool | None = None, description: str = "") -> dict[str, Any]:
    requires = action in {"research", "revise", "configure", "manual"} if requires_input is None else requires_input
    result: dict[str, Any] = {
        "id": action,
        "label": _ACTION_LABELS[action],
        "description": description or _ACTION_LABELS[action],
        "recommended": False,
        "kind": "action",
        "action": action,
        "requires_input": requires,
        "evidence": [],
        "verification": "suggested",
    }
    if requires:
        result["input_label"] = _ACTION_INPUT_LABELS[action]
    return result


def _closed_question(question: dict[str, Any]) -> bool:
    # The legacy enum contract remains closed when the caller explicitly says
    # so.  A missing allow_other is an open/dynamic question for which the UI
    # can offer the manual action.
    return bool(question.get("options")) and question.get("allow_other") is False


def _normalise_question_choices(
    question: dict[str, Any],
    model_choices: list[dict[str, Any]],
    *,
    context: dict[str, Any],
    roots: list[Path],
) -> list[dict[str, Any]]:
    name = question["name"]
    choices: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    closed = _closed_question(question)
    static_values = {
        _canonical_json(item.get("value"))
        for item in _original_options(question)
    }
    for index, raw in enumerate(model_choices):
        if not isinstance(raw, dict):
            raise _InvalidModelOutput(f"问题 {name} 的 choices[{index}] 必须是对象")
        kind = raw.get("kind") or ("action" if raw.get("action") else "value")
        # Explicitly closed legacy enums are authoritative.  Model-proposed
        # values cannot add an enum member, and the UI's Other/manual route
        # cannot turn that enum into a custom input.
        if closed and kind == "action" and raw.get("action") == "manual":
            continue
        if closed and kind == "value" and _canonical_json(raw.get("value")) not in static_values:
            continue
        choice_id = _choice_id(name, raw, index)
        if choice_id in seen_ids:
            choice_id = f"{choice_id}-{index + 1}"
        seen_ids.add(choice_id)
        normalised = (
            _normalise_action(name, raw, choice_id=choice_id)
            if kind == "action"
            else _normalise_value(name, raw, choice_id=choice_id, roots=roots)
        )
        _append_choice(choices, normalised)

    # Existing enum values are authoritative input choices.  They remain
    # available if a model only returned discovery actions.
    for index, raw in enumerate(_original_options(question)):
        raw["id"] = _choice_id(name, raw, index)
        _append_choice(choices, _normalise_value(name, raw, choice_id=raw["id"], roots=roots))

    for index, value in enumerate(_context_defaults(context, name)):
        if not isinstance(value, (str, list)):
            continue
        if closed and _canonical_json(value) not in static_values:
            continue
        raw = {
            "id": f"context-{index}",
            "label": "已有上下文中的值",
            "description": "来自现有上下文或图默认值",
            "recommended": False,
            "kind": "value",
            "value": value,
            "evidence": [],
        }
        _append_choice(choices, _normalise_value(name, raw, choice_id=raw["id"], roots=roots))

    question_default = question.get("default")
    if (
        question_default not in (None, "")
        and isinstance(question_default, (str, list))
        and (not closed or _canonical_json(question_default) in static_values)
    ):
        raw = {
            "id": "question-default",
            "label": "已有默认值",
            "description": "来自问题合同中的默认值",
            "recommended": False,
            "kind": "value",
            "value": _clone(question_default),
            "evidence": [],
        }
        _append_choice(choices, _normalise_value(name, raw, choice_id=raw["id"], roots=roots))

    if sum(bool(choice.get("recommended")) for choice in choices) > 1:
        raise _InvalidModelOutput(f"问题 {name} 最多只能有一个推荐候选")

    if not choices:
        if any(root.is_dir() for root in roots) or _research_clue(context, name):
            _append_choice(
                choices,
                _action_choice("research", description="请提供目录或材料线索后再准备实际候选值"),
            )
        _append_choice(choices, _action_choice("revise", description="补充或修改问题约束"))
        _append_choice(choices, _action_choice("configure", description="提供处理所需的配置说明"))
    for action in question.get("host_actions") or []:
        if action not in _ACTIONS:
            raise _InvalidModelOutput("宿主声明了不支持的处理动作")
        _append_choice(choices, _action_choice(action))

    # Every prepared question exposes Other. Its frozen enum constraints are
    # carried by the bundle and enforced before any custom value is bound.
    _append_choice(choices, _action_choice("manual",
        description="自己填写；回答仍须符合当前参数合同"))
    _append_choice(choices, _action_choice("defer"))
    return choices


def _research_clue(context: dict[str, Any], name: str) -> bool:
    if not isinstance(context, dict):
        return False
    for key in ("material_clues", "research_clues", "directories", "materials"):
        source = context.get(key)
        if isinstance(source, dict) and name in source:
            return bool(source[name])
        if isinstance(source, (list, tuple, str)) and source:
            return True
    return False


def _custom_contract(question: dict[str, Any]) -> dict[str, Any]:
    if not _closed_question(question):
        return {}
    return {"allowed_values": [_clone(item["value"]) for item in _original_options(question)]}


def _fallback_question(question: dict[str, Any], *, context: dict[str, Any], roots: list[Path]) -> list[dict[str, Any]]:
    return _normalise_question_choices(question, [], context=context, roots=roots)


def _choice_bundle(
    *,
    context_hash: str,
    questions: list[dict[str, Any]],
    model_questions: list[dict[str, Any]],
    context: dict[str, Any],
    roots: list[Path],
) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for item in model_questions:
        name = item["name"]
        if name not in {question["name"] for question in questions}:
            raise _InvalidModelOutput(f"模型返回了未声明问题: {name}")
        if name in by_name:
            raise _InvalidModelOutput(f"模型返回了重复问题: {name}")
        by_name[name] = item
    result: list[dict[str, Any]] = []
    for question in questions:
        model_question = by_name.get(question["name"], {})
        choices = _normalise_question_choices(question, model_question.get("choices", []), context=context, roots=roots)
        has_values = any(choice.get("kind") == "value" for choice in choices)
        mode = model_question.get("input_mode") or ("choice" if has_values else "text")
        if _closed_question(question):
            mode = "choice"
        result.append(
            {
                "name": question["name"],
                **{field: model_question[field] for field in ("question", "description") if field in model_question},
                "input_mode": mode,
                **_custom_contract(question),
                "choices": choices,
            }
        )
    return result


def _new_id() -> str:
    return f"choices-{uuid.uuid4().hex}"


def _source_fingerprint(prepared: list[dict[str, Any]], roots: list[Path]) -> list[dict[str, Any]]:
    """Hashable stat metadata for path values referenced by a bundle."""

    paths: set[str] = set()
    for question in prepared:
        for choice in question.get("choices", []) if isinstance(question, dict) else []:
            if not isinstance(choice, dict):
                continue
            candidates: list[Any] = []
            candidates.append(choice.get("value"))
            candidates.append(choice.get("evidence", []))
            for candidate in candidates:
                for item in _candidate_strings(candidate):
                    if _looks_like_path(item):
                        path = _path_for_verification(item, roots)
                        if path is not None:
                            paths.add(str(path))
                        elif Path(item).is_absolute() or PureWindowsPath(item).is_absolute():
                            unresolved = Path(item).expanduser().resolve(strict=False)
                            if _inside(unresolved, roots):
                                paths.add(str(unresolved))
                        else:
                            for root in roots:
                                unresolved = (root / item).resolve(strict=False)
                                if _inside(unresolved, roots):
                                    paths.add(str(unresolved))
                                    break
    fingerprint: list[dict[str, Any]] = []
    for raw in sorted(paths, key=os.path.normcase):
        path = Path(raw)
        try:
            stat = path.stat()
        except OSError:
            fingerprint.append({"path": raw, "exists": False})
            continue
        fingerprint.append(
            {
                "path": raw,
                "exists": True,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "is_file": path.is_file(),
                "is_dir": path.is_dir(),
            }
        )
    return fingerprint


def _bundle_version(
    *,
    identity: dict[str, Any],
    questions: list[dict[str, Any]],
    context_hash: str,
    status: str,
    error: str | None,
    roots: list[Path],
) -> str:
    return _sha256(
        {
            "identity": identity,
            "questions": questions,
            "context_hash": context_hash,
            "status": status,
            "error": error,
            "source_fingerprint": _source_fingerprint(questions, roots),
        }
    )


def _make_bundle(
    *,
    identity: dict[str, Any],
    questions: list[dict[str, Any]],
    context: dict[str, Any],
    roots: list[Path],
    status: str,
    error: str | None,
    prepared: list[dict[str, Any]],
    context_hash: str,
) -> dict[str, Any]:
    public = {
        "id": _new_id(),
        "version": "",
        "context_hash": context_hash,
        "status": status,
        "error": error,
        "questions": _clone(prepared),
        "allowed_roots": [str(root) for root in roots],
        "source_fingerprint": _source_fingerprint(prepared, roots),
    }
    public["version"] = _bundle_version(
        identity=identity,
        questions=public["questions"],
        context_hash=context_hash,
        status=status,
        error=error,
        roots=roots,
    )
    return public


def _failed_bundle(
    *,
    identity: dict[str, Any],
    questions: list[dict[str, Any]],
    context: dict[str, Any],
    roots: list[Path],
    context_hash: str,
    error: str,
) -> dict[str, Any]:
    fallback = [_fallback_question(question, context=context, roots=roots) for question in questions]
    prepared = [
        {"name": question["name"], "choices": choices, **_custom_contract(question)}
        for question, choices in zip(questions, fallback)
    ]
    return _make_bundle(
        identity=identity,
        questions=questions,
        context=context,
        roots=roots,
        status="failed",
        error=error,
        prepared=prepared,
        context_hash=context_hash,
    )


def _valid_cache(
    cached: Any,
    *,
    context_hash: str,
    identity: dict[str, Any],
    roots: list[Path],
) -> dict[str, Any] | None:
    if not isinstance(cached, dict) or cached.get("status") not in {"ready", "failed"}:
        return None
    if not cached.get("id") or not cached.get("version"):
        return None
    if cached.get("context_hash") != context_hash or not isinstance(cached.get("questions"), list):
        return None
    expected_version = _bundle_version(
        identity=identity,
        questions=cached["questions"],
        context_hash=context_hash,
        status=cached["status"],
        error=cached.get("error"),
        roots=roots,
    )
    if cached.get("version") != expected_version:
        return None
    return _clone(cached)


async def prepare_choices(
    *,
    identity: dict,
    questions: list[dict],
    context: dict,
    complete: Complete,
    allowed_roots: list[str],
    cached: dict | None = None,
    refresh: bool = False,
) -> dict:
    """Discover and prepare a versioned choice bundle.

    The callback is deliberately the only model boundary.  It receives a
    discovery prompt first and a metadata-only preparation prompt second.
    Invalid model output gets a targeted repair request; it is never silently
    truncated into a different schema.
    """

    if not isinstance(identity, dict) or not isinstance(context, dict) or not isinstance(questions, list):
        raise ValueError("identity、questions、context 的类型无效")
    roots = _normalise_roots(allowed_roots)
    clean_questions: list[dict[str, Any]] = []
    try:
        for index, question in enumerate(questions):
            if not isinstance(question, dict):
                raise ValueError(f"questions[{index}] 必须是对象")
            name = question.get("name")
            if not isinstance(name, str) or not name.strip() or name != name.strip():
                raise ValueError(f"questions[{index}].name 无效")
            if any(existing.get("name") == name for existing in clean_questions):
                raise ValueError(f"问题名称重复: {name}")
            clean_questions.append(_clone(question))
    except ValueError:
        raise

    context_hash = _context_hash(identity, clean_questions, context, roots)
    if not refresh:
        reused = _valid_cache(
            cached,
            context_hash=context_hash,
            identity=identity,
            roots=roots,
        )
        if reused is not None:
            return reused

    if not clean_questions:
        return _make_bundle(
            identity=identity,
            questions=[],
            context=context,
            roots=roots,
            status="ready",
            error=None,
            prepared=[],
            context_hash=context_hash,
        )

    discovery_system = (
        "你是编排选项的只读路径发现器。只能输出 JSON，不得调用命令、读取文件内容或查找密钥。"
        "允许的字段只有 searches:[{root,pattern}] 和 questions:[{name,choices}]。"
        "searches 中 root 必须来自允许目录，pattern 必须是受目录约束的 glob。"
    )
    discovery_prompt = _safe_prompt(
        {
            "task": "为问题寻找实际材料的路径线索；先规划只读 glob 搜索",
            "identity": identity,
            "questions": clean_questions,
            "context": context,
            "allowed_roots": [str(root) for root in roots],
            "output": {"searches": [{"root": "允许目录", "pattern": "例如 **/*.pdf"}], "questions": []},
        }
    )
    discovery, discovery_error = await _model_json(
        complete,
        system=discovery_system,
        prompt=discovery_prompt,
        validator=_validate_discovery,
        phase="路径发现",
    )
    if discovery_error:
        return _failed_bundle(
            identity=identity,
            questions=clean_questions,
            context=context,
            roots=roots,
            context_hash=context_hash,
            error=discovery_error,
        )
    searches, _ = discovery
    metadata, rejected = _discover_files(searches, roots)

    prepare_system = (
        "你是编排选项准备器。只能输出 JSON，不能执行命令或猜测文件存在。"
        "先分析问题合同和当前上下文，像计划模式一样找出需要用户决定的事项，再生成可直接选择的方案。"
        "返回 questions:[{name,question,description,input_mode,choices:[...]}]。"
        "question用自然语言问清本次具体要决定什么；description简要解释背景和影响，不展示内部字段名或授权记录编号。"
        "优先使用input_mode=choice：范围、服务、模型、费用取舍、是否继续等尽量给出2至3个明确选项，"
        "每项的label和description说明具体内容、影响与已知边界，可以推荐但不能替用户选择。"
        "只有缺少用户独有的路径、具体数值、材料或自由描述，且无法从上下文生成有意义的可选值时才用input_mode=text，"
        "并说明需要输入什么。已有候选路径、数值或材料也应优先给选项，不要求用户重复填写。"
        "choice.kind 只能是 value 或 action；action 只能是 research、revise、configure、"
        "retry_prepare、defer、manual。value 必须是文本或文本列表。"
        "不发明未知的服务、模型、页码、费用额度或授权；决定范围与真实工具授权仍由原有宿主流程核验。"
        "不要为了凑数量制造候选；文件路径只有在元数据支持时才可声称 verified。"
    )
    prepare_prompt = _safe_prompt(
        {
            "task": "根据当前上下文生成用户能直接回答的问题和选项；仅对确需自由输入的事项使用文本输入",
            "identity": identity,
            "questions": clean_questions,
            "context": context,
            "discovered_files": metadata,
            "rejected_searches": rejected,
            "output": {"questions": [{"name": "问题名", "question": "本次具体需要你决定什么？",
                                       "description": "背景及选择的影响", "input_mode": "choice", "choices": []}]},
        }
    )
    prepared_model, prepare_error = await _model_json(
        complete,
        system=prepare_system,
        prompt=prepare_prompt,
        validator=_validate_choices,
        phase="选项准备",
    )
    if prepare_error:
        return _failed_bundle(
            identity=identity,
            questions=clean_questions,
            context=context,
            roots=roots,
            context_hash=context_hash,
            error=prepare_error,
        )
    _, model_questions = prepared_model
    try:
        prepared_questions = _choice_bundle(
            context_hash=context_hash,
            questions=clean_questions,
            model_questions=model_questions,
            context=context,
            roots=roots,
        )
    except _InvalidModelOutput as exc:
        return _failed_bundle(
            identity=identity,
            questions=clean_questions,
            context=context,
            roots=roots,
            context_hash=context_hash,
            error=str(exc),
        )
    return _make_bundle(
        identity=identity,
        questions=clean_questions,
        context=context,
        roots=roots,
        status="ready",
        error=None,
        prepared=prepared_questions,
        context_hash=context_hash,
    )


def project_questions(questions: list[dict], bundle: dict) -> list[dict]:
    """Project prepared choices into the legacy input presentation contract."""

    if not isinstance(questions, list):
        raise ValueError("questions 必须是列表")
    if not isinstance(bundle, dict):
        raise ValueError("bundle 必须是对象")
    by_name = {
        item.get("name"): item
        for item in bundle.get("questions", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    result: list[dict[str, Any]] = []
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("问题必须是对象")
        projected = _clone(question)
        prepared = by_name.get(question.get("name"))
        if prepared is None or not isinstance(prepared.get("choices"), list):
            result.append(projected)
            continue
        choices = [choice for choice in prepared["choices"] if isinstance(choice, dict)]
        projected["options"] = [
            {
                "value": choice["id"],
                "label": choice.get("label", ""),
                "description": choice.get("description", ""),
                "recommended": bool(choice.get("recommended", False)),
            }
            for choice in choices
        ]
        projected["prepared_choices"] = _clone(choices)
        for field in ("input_mode", "description"):
            if field in prepared:
                projected[field] = prepared[field]
        if prepared.get("question"):
            projected["message"] = prepared["question"]
            projected["question"] = prepared["question"]
        projected["allow_other"] = True
        if "allowOther" in projected:
            projected["allowOther"] = True
        if "allowed_values" in prepared:
            projected["closed_values"] = _clone(prepared["allowed_values"])
        # A recommendation is display metadata; it must never become an
        # implicit answer.  Keeping an explicit null is easier for consumers
        # that distinguish absent and intentionally empty defaults.
        projected["default"] = None
        result.append(projected)
    return result


def _input_for(
    name: str,
    choice_id: str,
    choice_inputs: dict[str, Any],
    direct: Any = None,
) -> Any:
    if direct is not None:
        return direct
    if choice_id in choice_inputs:
        value = choice_inputs[choice_id]
        if isinstance(value, dict):
            return value.get("input", value.get("value"))
        return value
    if name not in choice_inputs:
        return None
    value = choice_inputs[name]
    if isinstance(value, dict):
        if choice_id in value:
            nested = value[choice_id]
            if isinstance(nested, dict):
                return nested.get("input", nested.get("value"))
            return nested
        return value.get("input", value.get("value"))
    return value


def _answer_tokens(answer: Any) -> list[tuple[Any, Any]]:
    if isinstance(answer, dict):
        token = answer.get("id", answer.get("choice_id", answer.get("value")))
        if token is None:
            raise ValueError("答案对象缺少 choice id")
        return [(token, answer.get("input"))]
    if isinstance(answer, list):
        return [(item, None) for item in answer]
    return [(answer, None)]


def _valid_custom(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip()) and value == value.strip() and "\x00" not in value
    if isinstance(value, list):
        return bool(value) and all(_valid_custom(item) for item in value)
    return False


def resolve_answers(
    bundle: dict,
    answers: dict,
    *,
    bundle_id: str = "",
    version: str = "",
    choice_inputs: dict | None = None,
) -> dict:
    """Resolve projected choice ids without allowing actions to bind values."""

    if not isinstance(bundle, dict) or bundle.get("status") not in {"ready", "failed"}:
        raise ValueError("选项包不可用")
    if bundle_id and bundle.get("id") != bundle_id:
        raise ValueError("选项包 id 已过期")
    if version and bundle.get("version") != version:
        raise ValueError("选项包版本已过期")
    if not isinstance(answers, dict):
        raise ValueError("answers 必须是对象")
    if choice_inputs is None:
        choice_inputs = {}
    if not isinstance(choice_inputs, dict):
        raise ValueError("choice_inputs 必须是对象")
    by_name = {
        question.get("name"): question
        for question in bundle.get("questions", [])
        if isinstance(question, dict) and isinstance(question.get("name"), str)
    }
    values: dict[str, str | list] = {}
    actions: list[dict[str, Any]] = []
    for name, answer in answers.items():
        if name not in by_name:
            raise ValueError(f"答案对应的问题不存在: {name}")
        question = by_name[name]
        choices = question.get("choices")
        if not isinstance(choices, list):
            raise ValueError(f"问题 {name} 的选项包无效")
        by_id = {choice.get("id"): choice for choice in choices if isinstance(choice, dict)}
        manual_available = any(
            choice.get("kind") == "action" and choice.get("action") == "manual"
            for choice in choices
            if isinstance(choice, dict)
        )
        value_results: list[Any] = []
        allowed_values = question.get("allowed_values")
        def validate_custom(value):
            if not manual_available or not _valid_custom(value):
                raise ValueError(f"问题 {name} 不接受这个自定义回答")
            if isinstance(allowed_values, list):
                allowed = {_canonical_json(item) for item in allowed_values}
                items = value if isinstance(value, list) else [value]
                if any(_canonical_json(item) not in allowed for item in items):
                    raise ValueError(f"问题 {name} 的回答不符合原参数合同；请选择现有值或调整方案")
        if isinstance(answer, dict) and set(answer) == {'custom'}:
            validate_custom(answer['custom'])
            value_results.append(_clone(answer['custom']))
            tokens = []
        else:
            tokens = _answer_tokens(answer)
        for token, direct_input in tokens:
            if isinstance(token, str) and token in by_id:
                choice = by_id[token]
                if choice.get("kind") == "action":
                    action = choice.get("action")
                    if action not in _ACTIONS:
                        raise ValueError(f"问题 {name} 的动作不受支持")
                    input_value = _input_for(name, token, choice_inputs, direct_input)
                    if choice.get("requires_input") and not _valid_custom(input_value):
                        raise ValueError(f"动作 {action} 需要补充输入")
                    actions.append({"name": name, "action": action, "input": input_value})
                else:
                    value_results.append(_clone(choice.get("value")))
                continue

            # A raw answer is a custom value only when the prepared bundle
            # explicitly exposes the manual/Other route. The frozen contract
            # still rejects values outside an original closed enum.
            validate_custom(token)
            value_results.append(_clone(token))

        if value_results:
            flattened: list[Any] = []
            had_list_value = False
            for value in value_results:
                if isinstance(value, list):
                    had_list_value = True
                    flattened.extend(_clone(value))
                else:
                    flattened.append(_clone(value))
            if isinstance(answer, list) or had_list_value or len(flattened) != 1:
                values[name] = flattened
            else:
                values[name] = flattened[0]
    if values and "source_fingerprint" in bundle:
        current = _source_fingerprint(bundle.get("questions", []),
                                      [Path(root) for root in bundle.get("allowed_roots", [])])
        if current != bundle['source_fingerprint']:
            raise ValueError("候选文件已变化或删除，请重新准备选项；未使用旧文件证据")
    return {"values": values, "actions": actions}


__all__ = ["prepare_choices", "project_questions", "resolve_answers"]
