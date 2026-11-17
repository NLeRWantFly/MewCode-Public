"""Host-owned preparation of safe choices for orchestration interactions.

The shared :mod:`mewcode.orchestration.interaction_choices` module owns the
model output contract.  This module owns the desktop boundary around that
contract: selecting the question source, checking conversation/run/canvas
identity, freezing a bounded context, and persisting a prepared bundle only
after the same request is still current.

Preparation is deliberately separate from answering.  A choice id is a
temporary presentation id; callers that need to answer a graph question must
use :func:`resolve_graph_selections` to recover the canonical value before
passing it to the existing discussion protocol.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from fastapi import Request
from fastapi.responses import JSONResponse

from mewcode.orchestration.extract import collect_model_text
from mewcode.orchestration.interaction_choices import (
    _context_hash as _core_context_hash,
    prepare_choices,
    project_questions,
    resolve_answers,
)
from mewcode.orchestration.spec import OrchestrationSpec


_ROUTE = "/api/orchestration/interaction-choices/prepare"
_MAX_INSTRUCTION = 8000
_MAX_CONTEXT_TEXT = 2000
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\x00\r\n]+|/[A-Za-z0-9._~+\-][^\x00\r\n]*)"
)
_SENSITIVE = re.compile(
    r"(?:password|passwd|secret|credential|token|api[_-]?key|private[_-]?key|access[_-]?key|cookie)",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|authorization)\b"
    r"\s*[:=]\s*[^\s,;，；]+|\bBearer\s+[A-Za-z0-9._~+/=-]+"
)
_FULL_CONTENT_FIELD = re.compile(
    r"(?i)(?:pdf|document|raw[_-]?(?:text|bytes|content)|file[_-]?content|binary)"
)
_PATH_FIELDS = {
    "path",
    "paths",
    "file",
    "files",
    "directory",
    "directories",
    "input_path",
    "input_paths",
    "source_path",
    "source_paths",
    "authorized_paths",
    "actual_input_paths",
    "research_root",
    "research_roots",
}


class _ScopeChanged(ValueError):
    """The request changed while a model call was in flight."""


def _redact_text(value: Any) -> str:
    return _SECRET_TEXT.sub("<redacted>", str(value))


def _json_safe(value: Any, *, depth: int = 0, key: str = "") -> Any:
    """Make a bounded, non-secret JSON-shaped copy for hashes and prompts."""

    if depth > 6:
        return "<truncated>"
    if _FULL_CONTENT_FIELD.search(key or ""):
        return "<content omitted>"
    if _SENSITIVE.search(key or ""):
        return "<redacted>"
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, str):
            return _redact_text(value)[:_MAX_CONTEXT_TEXT]
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(k): _json_safe(v, depth=depth + 1, key=str(k))
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
            if not _SENSITIVE.search(str(k))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
        return [_json_safe(item, depth=depth + 1, key=key) for item in values[:256]]
    return repr(value)[:_MAX_CONTEXT_TEXT]


def _canonical(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _ensure_failed_retry_action(bundle: dict[str, Any]) -> dict[str, Any]:
    """Keep a failed, visible card recoverable without binding a value."""

    if bundle.get("status") != "failed":
        return bundle
    for question in bundle.get("questions") or []:
        if not isinstance(question, dict) or not isinstance(question.get("choices"), list):
            continue
        if any(isinstance(choice, dict) and choice.get("action") == "retry_prepare"
               for choice in question["choices"]):
            continue
        question["choices"].append({
            "id": "retry_prepare",
            "label": "重新准备选项",
            "description": "重新查找并准备候选值",
            "recommended": False,
            "kind": "action",
            "action": "retry_prepare",
            "requires_input": False,
            "evidence": [],
            "verification": "suggested",
        })
    return bundle


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _host_state(srv: Any) -> dict[str, Any]:
    """Return host-local state without a process-global conversation cache."""

    state = getattr(srv, "_interaction_choice_state", None)
    if not isinstance(state, dict):
        state = {
            "locks": {},
            "cache": {},
            "startup": {},
            "graph": {},
        }
        try:
            setattr(srv, "_interaction_choice_state", state)
        except Exception:
            # A small fake host may use slots.  ``prepare_for_context`` still
            # works without persistence; endpoint tests normally provide an
            # extensible host.
            return state
    for name, default in (("locks", {}), ("cache", {}), ("startup", {}), ("graph", {})):
        if not isinstance(state.get(name), dict):
            state[name] = default
    return state


def _active_cid(srv: Any) -> str:
    function = getattr(srv, "_active_conversation_id", None)
    if callable(function):
        try:
            return _text(function())
        except TypeError:
            pass
    session = getattr(srv, "SESSION", None)
    value = getattr(session, "conversation_id", "")
    if not value:
        value = getattr(getattr(session, "session", None), "session_id", "")
    return _text(value)


def _scope_cid(srv: Any, runtime: Any = None) -> str:
    """Return the runtime conversation when preparing background work."""

    runtime_cid = _text(_runtime_value(runtime, "conversation_id") or
                        _runtime_value(runtime, "cid"))
    return runtime_cid or _active_cid(srv)


def _workdir(srv: Any, runtime: Any = None) -> Path:
    # A background orchestration runtime may belong to a non-foreground
    # conversation/project.  Its own workdir is authoritative for preparation;
    # the foreground session is only the fallback used by startup HTTP calls.
    value = getattr(runtime, "work_dir", None)
    if not value:
        value = getattr(getattr(srv, "SESSION", None), "work_dir", None)
    if not value and isinstance(runtime, dict):
        value = runtime.get("work_dir")
    return Path(value or os.getcwd()).expanduser().resolve(strict=False)


def _runtime_for(srv: Any, cid: str) -> Any:
    session = getattr(srv, "SESSION", None)
    function = getattr(session, "runtime_for", None)
    if callable(function):
        try:
            return function(cid)
        except TypeError:
            return function()
    function = getattr(srv, "runtime_for", None)
    if callable(function):
        try:
            return function(cid)
        except TypeError:
            return function()
    # Some desktop hosts keep the runtime accessor private beside the other
    # orchestration helpers.  It is still the host-owned runtime for this
    # conversation, so use it before falling back to the failed-model path.
    function = getattr(srv, "_runtime_for", None)
    if callable(function):
        try:
            return function(cid)
        except TypeError:
            return function()
    return None


def _runtime_value(runtime: Any, key: str, default: Any = None) -> Any:
    if isinstance(runtime, dict):
        return runtime.get(key, default)
    return getattr(runtime, key, default)


def _run_store(srv: Any) -> Any:
    for name in ("EXPERT_RUNS", "run_store", "RUN_STORE"):
        value = getattr(srv, name, None)
        if value is not None:
            return value
    return None


def _load_run(srv: Any, run_id: str) -> dict[str, Any] | None:
    if not run_id:
        return None
    store = _run_store(srv)
    if store is None:
        return None
    for name in ("load", "get", "read"):
        function = getattr(store, name, None)
        if callable(function):
            value = function(run_id)
            if inspect.isawaitable(value):
                raise ValueError("运行快照读取不得异步跨越候选准备边界")
            return _clone(value) if isinstance(value, dict) else value
    return None


def _project_key(srv: Any) -> str:
    return str(_workdir(srv))


def _current_spec(srv: Any, cid: str, supplied: Any = None) -> Any:
    """Obtain a host-owned spec when the host exposes its normal accessors."""

    for name in ("_orchestration_spec_for_project", "current_orchestration_spec", "current_spec"):
        function = getattr(srv, name, None)
        if not callable(function):
            continue
        for args in ((), (cid,)):
            try:
                value = function(*args)
                if value is not None:
                    if hasattr(value, "as_dict"):
                        return value.as_dict()
                    if isinstance(value, dict):
                        return _clone(value)
            except (TypeError, ValueError, AttributeError):
                continue
    if supplied is not None and supplied != {}:
        return _clone(supplied.as_dict() if hasattr(supplied, "as_dict") else supplied)
    return None


def _owned_spec(srv: Any, value: Any, cid: str) -> OrchestrationSpec:
    try:
        spec = OrchestrationSpec.from_value(value)
    except Exception as exc:
        raise ValueError(f"编排方案无效：{exc}") from exc
    own = getattr(srv, "_own_orchestration_spec", None)
    if callable(own):
        try:
            result = own(spec, conversation_id=cid, project=_project_key(srv))
        except TypeError:
            try:
                result = own(spec)
            except Exception as exc:
                raise ValueError(str(exc)) from exc
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        if result is not None:
            spec = result
    return OrchestrationSpec.from_value(spec)


def _spec_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    return _clone(value) if isinstance(value, dict) else {}


def _normalised_spec_dict(value: Any) -> dict[str, Any]:
    """Use the host spec's canonical shape when comparing supplied specs."""

    if isinstance(value, OrchestrationSpec):
        return _spec_dict(value)
    try:
        return _spec_dict(OrchestrationSpec.from_value(value))
    except (TypeError, ValueError, AttributeError):
        return _spec_dict(value)


def _graph_hash(value: Any) -> str:
    return _digest(value or {})


def _node_stage(snapshot: dict[str, Any] | None, node_id: str) -> dict[str, Any]:
    states = (snapshot or {}).get("node_states") or {}
    value = states.get(node_id) if isinstance(states, dict) else None
    return value if isinstance(value, dict) else {}


def _pending_request(runtime: Any, request_id: str) -> tuple[Any, str]:
    """Return a live request and its kind without accepting completed futures."""

    if runtime is None:
        return None, ""
    for name, kind in (("human_oncall_request", "human_oncall"), ("ask_user_request", "ask_user"),
                       ("pending_request", "interaction")):
        request = _runtime_value(runtime, name)
        if request is None:
            continue
        actual = _text(getattr(request, "request_id", "") if not isinstance(request, dict)
                       else request.get("request_id"))
        if request_id and actual != request_id:
            continue
        future = getattr(request, "future", None) if not isinstance(request, dict) else request.get("future")
        if future is not None and getattr(future, "done", lambda: False)():
            continue
        return request, kind
    return None, ""


def _request_questions(request: Any) -> list[dict[str, Any]]:
    if request is None:
        return []
    if isinstance(request, dict):
        values = request.get("source_questions") or request.get("questions") or []
    else:
        values = getattr(request, "source_questions", None) or getattr(request, "questions", None) or []
    return _normalise_questions(values)


def _normalise_option(option: Any) -> dict[str, Any] | None:
    if isinstance(option, str):
        value = _redact_text(option).strip()
        return {"value": value, "label": value} if value else None
    if not isinstance(option, dict):
        return None
    value = option.get("value", option.get("id"))
    if not isinstance(value, (str, list)) or not value:
        return None
    label = option.get("label", value if isinstance(value, str) else "候选值")
    if not isinstance(label, str) or not label.strip():
        label = str(value)
    if isinstance(value, list):
        value = [_redact_text(item) if isinstance(item, str) else _clone(item) for item in value]
    else:
        value = _redact_text(value)
    result = {"value": _clone(value), "label": _redact_text(label).strip()}
    for key in ("description", "recommended"):
        if key in option:
            result[key] = _redact_text(option[key]) if key == "description" else _clone(option[key])
    return result


def _normalise_questions(values: Any) -> list[dict[str, Any]]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("问题合同必须是列表")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"问题合同第 {index + 1} 项无效")
        name = _text(raw.get("name") or raw.get("id") or f"question-{index + 1}")
        if not name or name in seen:
            raise ValueError("问题名称缺失或重复")
        seen.add(name)
        result_row: dict[str, Any] = {"name": name}
        label = _text(raw.get("label") or raw.get("title") or raw.get("header"))
        message = _text(raw.get("message") or raw.get("question"))
        description = _text(raw.get("description") or raw.get("context") or message)
        if label:
            result_row["label"] = _redact_text(label)[:300]
        if message:
            result_row["message"] = _redact_text(message)[:_MAX_CONTEXT_TEXT]
        if description:
            result_row["description"] = _redact_text(description)[:_MAX_CONTEXT_TEXT]
        for key in ("type", "input_type", "separator", "host_actions", "required", "default", "collect_at", "required_when"):
            if key in raw:
                result_row[key] = (_json_safe(raw[key]) if key in {"default", "required_when"}
                                   else _clone(raw[key]))
        options = raw.get("options", raw.get("choices", []))
        normal_options = [item for item in (_normalise_option(option) for option in options or []) if item]
        if normal_options:
            result_row["options"] = normal_options
        if "allow_other" in raw:
            result_row["allow_other"] = bool(raw["allow_other"])
        elif "allowOther" in raw:
            result_row["allow_other"] = bool(raw["allowOther"])
        elif "allow_other" not in result_row:
            # HumanOnCall questions explicitly allow a custom route; typed
            # input fields with options remain closed unless their contract
            # says otherwise.
            result_row["allow_other"] = not bool(normal_options and raw.get("type") in {"radio", "select"})
        result.append(result_row)
    return result


def _contract_questions_from_spec(spec: OrchestrationSpec, values: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    values = values if isinstance(values, dict) else {}
    contract: Any = None
    if spec.goal_contract is not None:
        contract = list(spec.goal_contract.run_inputs)
    if not contract:
        contract = (spec.meta or {}).get("run_input_contract") or []
    if not isinstance(contract, list):
        contract = []
    declarations = [item for item in contract if isinstance(item, dict)]
    missing: list[dict[str, Any]] = []
    try:
        from mewcode.orchestration.run_inputs import stage_input_questions
        missing, normalized = stage_input_questions(declarations, values, "start")
    except Exception:
        normalized = _clone(values)
        for item in declarations:
            name = _text(item.get("name"))
            if item.get("collect_at", "start") == "start" and not _value_present(normalized.get(name)):
                if item.get("required", True) or item.get("required_when"):
                    missing.append(item)
    missing_names = {_text(item.get("name")) for item in missing if isinstance(item, dict)}
    declarations_for_questions = []
    for declaration in declarations:
        if declaration.get("collect_at", "start") != "start":
            continue
        name = _text(declaration.get("name"))
        # Required gaps are the primary startup questions.  A value already
        # known by the host, or a non-empty declared default, stays visible as
        # a candidate so the user can confirm it; neither becomes a selection.
        if (name in missing_names or _value_present(normalized.get(name))
                or _value_present(declaration.get("default"))):
            declarations_for_questions.append(declaration)
    questions = _normalise_questions(declarations_for_questions)
    # Keep known values and defaults as context candidates.  A value is never
    # marked recommended and no answer is selected by this preparation layer.
    for question, declaration in zip(questions, declarations_for_questions):
        if declaration.get("options"):
            question["allow_other"] = False
        name = question["name"]
        if name in normalized and _value_present(normalized[name]):
            question.setdefault("default", _clone(normalized[name]))
        elif "default" in declaration and _value_present(declaration.get("default")):
            question.setdefault("default", _clone(declaration["default"]))
    return questions, normalized


def _value_present(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value)
    return bool(str(value or "").strip())


def _case_from_snapshot(snapshot: dict[str, Any] | None, case_id: str, node_id: str) -> dict[str, Any] | None:
    if not case_id or not isinstance(snapshot, dict):
        return None
    # RecoveryCase is ledger-owned.  Legacy top-level ``cases`` arrays are
    # intentionally ignored because they can lag the append-only case fact.
    try:
        from mewcode.experts.recovery_case import get_case
        ledger_case = get_case(snapshot, case_id)
        if isinstance(ledger_case, dict) and (not node_id or ledger_case.get("node_id") == node_id):
            return ledger_case
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        pass
    stage = _node_stage(snapshot, node_id)
    direct = stage.get("recovery_case")
    if isinstance(direct, dict) and direct.get("case_id") == case_id:
        return direct
    return None


def _case_questions(case: dict[str, Any], node_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pending = case.get("pending") if isinstance(case.get("pending"), dict) else {}
    values = pending.get("source_questions") or pending.get("questions") or pending.get("input_definitions") or []
    questions = _normalise_questions(values)
    if questions:
        return questions, pending
    recovery = pending.get("recovery_options") or case.get("report", {}).get("recovery_options") or []
    if not isinstance(recovery, list):
        recovery = []
    options: list[dict[str, Any]] = []
    for item in recovery:
        if not isinstance(item, dict):
            continue
        value = item.get("value", item.get("option_id"))
        if not isinstance(value, str) or not value.strip():
            continue
        options.append({"value": value, "label": item.get("label") or value,
                        "description": item.get("description") or item.get("method") or ""})
    return ([{"name": "recovery_action", "label": "恢复动作", "message": pending.get("question", "请选择恢复动作"),
              "options": options, "allow_other": True,
               "host_actions": ["defer"]}], pending)


def _case_choice_stamp(case: dict[str, Any]) -> str:
    return _digest({key: case.get(key) for key in
                   ('case_id', 'status', 'epoch', 'input_revision', 'rounds',
                    'pending', 'pending_choice_action', 'closed_by_user')})


def _graph_questions(document: dict[str, Any]) -> list[dict[str, Any]]:
    from mewcode.orchestration.graph_discussion import issue_rows

    rows = issue_rows(document or {})
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "business":
            continue
        options = row.get("options") if isinstance(row.get("options"), list) else []
        result.append({
            "name": _text(row.get("id")),
            "label": _text(row.get("title") or row.get("label") or row.get("id")),
            "message": _text(row.get("body")),
            "description": _text(row.get("body")),
            "options": [_normalise_option(item) for item in options if _normalise_option(item)],
            "allow_other": not bool(options),
        })
    return [item for item in result if item["name"]]


def _safe_graph_summary(graph: Any) -> dict[str, Any]:
    if hasattr(graph, "as_dict"):
        graph = graph.as_dict()
    if not isinstance(graph, dict):
        return {}
    nodes = []
    for item in graph.get("nodes") or []:
        if not isinstance(item, dict):
            continue
        nodes.append({key: _json_safe(item.get(key)) for key in ("node_id", "id", "kind", "label", "goal") if item.get(key) is not None})
    edges = []
    for item in graph.get("edges") or []:
        if not isinstance(item, dict):
            continue
        edges.append({key: _json_safe(item.get(key)) for key in ("from_node", "to_node", "condition", "input_when", "branch_group") if item.get(key) is not None})
    return {
        "hash": _graph_hash(graph),
        "spec_id": _json_safe(graph.get("spec_id") or graph.get("id")),
        "label": _json_safe(graph.get("label")),
        "goal": _json_safe(graph.get("goal")),
        "nodes": nodes[:200],
        "edges": edges[:400],
    }


def _auth_summary(snapshot: dict[str, Any] | None, runtime: Any = None) -> dict[str, Any]:
    values: list[Any] = []
    if isinstance(snapshot, dict):
        authorizations = snapshot.get("authorizations") or snapshot.get("authorization") or {}
        if isinstance(authorizations, dict):
            values = authorizations.get("entries") or authorizations.get("grants") or []
        elif isinstance(authorizations, list):
            values = authorizations
    if not values and runtime is not None:
        values = _runtime_value(runtime, "authorizations", []) or []
    rows = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        row = {key: _json_safe(item.get(key)) for key in ("auth_id", "kind", "quote", "scope",
            "operation_class", "host_verified", "enforceable", "expires_at", "revoked_at") if key in item}
        expires = item.get("expires_at")
        row["active"] = bool(item.get("enforceable", item.get("host_verified", False))
            and not item.get("revoked_at") and (not expires or float(expires) > time.time()))
        rows.append(row)
    return {"count": len(rows), "entries": rows, "hash": _digest(rows)}


def _service_metadata(srv: Any, runtime: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    client = _runtime_value(runtime, "client") if runtime is not None else None
    provider = _runtime_value(runtime, "provider") if runtime is not None else None
    for source in (provider, client):
        if source is None:
            continue
        for key in ("name", "model", "model_id", "provider_key", "effort"):
            value = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
            if value and key not in result:
                # Provider keys may be account secrets in desktop adapters.
                # Keep only an opaque digest for scope/debug identity; model
                # and effort remain useful routing metadata.
                result["provider_key_hash" if key == "provider_key" else key] = (
                    _digest(_text(value)) if key == "provider_key" else _text(value)
                )
    context_fn = getattr(srv, "_orchestration_attempt_context", None)
    if callable(context_fn) and not client:
        try:
            value = context_fn("")
            if isinstance(value, dict):
                for key in ("provider_key", "provider_name", "model", "requested_effort", "policy"):
                    if value.get(key) is not None:
                        target = "provider_key_hash" if key == "provider_key" else key
                        result[target] = (_digest(_text(value[key])) if key == "provider_key"
                                          else _json_safe(value[key]))
        except Exception:
            # A missing model is handled by the core failed fallback.  Merely
            # inspecting metadata must not make the API perform a paid call.
            pass
    return result


def _safe_paths(value: Any, *, field: str = "", result: set[Path] | None = None) -> set[Path]:
    result = result if result is not None else set()
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE.search(key_text):
                continue
            _safe_paths(item, field=key_text, result=result)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _safe_paths(item, field=field, result=result)
        return result
    if not isinstance(value, (str, os.PathLike)):
        return result
    text = os.fspath(value)
    if "\x00" in text or len(text) > 1000:
        return result
    looks_path = field.casefold() in _PATH_FIELDS or Path(text).is_absolute() or "/" in text or "\\" in text
    if not looks_path:
        return result
    path = Path(text).expanduser().resolve(strict=False)
    try:
        if path.exists():
            root = path if path.is_dir() else path.parent
            # Never widen discovery to a drive/filesystem root merely because
            # a broad path was present in an input declaration.
            if root.parent != root:
                result.add(root)
    except OSError:
        pass
    return result


def _instruction_roots(instruction: str) -> set[Path]:
    roots: set[Path] = set()
    for match in _PATH_RE.findall(instruction or ""):
        path = Path(match).expanduser().resolve(strict=False)
        try:
            if path.exists() and path.is_dir():
                if path.parent != path:
                    roots.add(path)
        except OSError:
            continue
    return roots


def _allowed_roots(srv: Any, runtime: Any, snapshot: dict[str, Any] | None,
                   questions: list[dict[str, Any]], instruction: str) -> list[str]:
    workdir = _workdir(srv, runtime)
    roots: set[Path] = {workdir}
    values = (snapshot or {}).get("run_inputs") if isinstance(snapshot, dict) else {}
    roots.update(_safe_paths(values, field="run_inputs"))
    if isinstance(snapshot, dict):
        for key in ("actual_input_paths", "authorized_input_paths", "input_paths", "source_paths"):
            roots.update(_safe_paths(snapshot.get(key), field=key))
    roots.update(_safe_paths(questions, field="options"))
    # A research path is extra scope only when the user supplied a concrete
    # existing directory in this request.  It is never inferred from cwd's
    # parent or from a broad drive root.
    if instruction:
        roots.update(_instruction_roots(instruction))
    return sorted({str(root) for root in roots}, key=os.path.normcase)


def _dialogue_evidence(srv: Any, runtime: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Reuse the existing effective draft and authored chat; never grant authority."""
    candidate = snapshot.get('candidate') or {}
    if not isinstance(candidate, dict) or not candidate.get('document'):
        draft = _draft_state(srv, _scope_cid(srv, runtime)) or {}
        candidate = draft.get('candidate') or {}
    document = candidate.get('document') if isinstance(candidate, dict) else {}
    document = document if isinstance(document, dict) else {}
    entries = []
    for item in (document or {}).get('entries') or []:
        if not isinstance(item, dict) or item.get('state', 'active') != 'active':
            continue
        if item.get('kind') not in {'goal', 'requirement', 'constraint', 'decision', 'input', 'node'}:
            continue
        entries.append({key: _json_safe(item[key]) for key in
            ('id', 'kind', 'title', 'body', 'data', 'evidence', 'sources') if key in item})
    conversation = _runtime_value(runtime, 'conversation')
    history = conversation.get('history', []) if isinstance(conversation, dict) else getattr(conversation, 'history', [])
    authored = []
    for message in history or []:
        def field(key, default=None):
            return message.get(key, default) if isinstance(message, dict) else getattr(message, key, default)
        role = field('role', '')
        if role not in {'user', 'assistant'} or field('transient', False):
            continue
        content = field('content', '')
        if isinstance(content, list):
            content = '\n'.join(str(block.get('text') or '') for block in content
                                if isinstance(block, dict) and block.get('type') == 'text')
        if isinstance(content, str) and content.strip():
            authored.append({'role': role, 'text': _redact_text(content), 'source': 'existing_chat'})
    return {'effective_entries': entries, 'authored_chat': authored,
            'use': '背景和候选线索；助手建议和引用材料不是执行授权'}


def _context_for(srv: Any, runtime: Any, snapshot: dict[str, Any] | None,
                 questions: list[dict[str, Any]], *, graph: Any = None,
                 instruction: str = "", node_id: str = "") -> dict[str, Any]:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    stage = _node_stage(snapshot, node_id)
    pending = stage.get("pending_interaction") if isinstance(stage.get("pending_interaction"), dict) else {}
    params = snapshot.get("run_inputs") if isinstance(snapshot.get("run_inputs"), dict) else {}
    upstream = stage.get("upstream_sources") or stage.get("handoff") or snapshot.get("upstream_sources") or {}
    case = stage.get('recovery_case') or {}
    report = case.get('report') or {}
    plan = case.get('plan') or {}
    safe_upstream = []
    if isinstance(upstream, dict):
        for key, value in upstream.items():
            if not isinstance(value, dict):
                continue
            row = {"node_id": _text(value.get("node_id") or key)}
            handoff = value.get("handoff") if isinstance(value.get("handoff"), dict) else value
            artifacts = handoff.get("artifacts") if isinstance(handoff, dict) else []
            if isinstance(artifacts, list):
                row["artifacts"] = [
                    {field: _json_safe(item.get(field)) for field in ("kind", "name", "path", "note") if item.get(field) is not None}
                    for item in artifacts if isinstance(item, dict)
                ][:100]
            safe_upstream.append(row)
    return {
        "graph": _safe_graph_summary(graph or snapshot.get("locked_flow") or snapshot.get("spec") or {}),
        "parameters": _json_safe(params),
        "defaults": {q.get("name"): _json_safe(q.get("default")) for q in questions if "default" in q},
        "upstream_handoffs": safe_upstream,
        "service": _service_metadata(srv, runtime),
        "authorizations": _auth_summary(snapshot, runtime),
        "node": {"node_id": node_id, "input_revision": _json_safe(stage.get("input_revision"))},
        "recovery": _json_safe({
            'case_id': case.get('case_id'),
            'root_cause': plan.get('root_cause') or report.get('root_cause'),
            'failure': {'reason': report.get('reason'), 'error_kind': stage.get('error_kind'),
                        'stop_reason': stage.get('stop_reason'), 'signature': case.get('signature')},
            'methods': [{key: option.get(key) for key in
                         ('option_id', 'method', 'needs', 'verification')}
                        for option in plan.get('options', []) if isinstance(option, dict)],
        }),
        "instruction": _redact_text(instruction),
        "dialogue": _dialogue_evidence(srv, runtime, snapshot),
    }


def _identity_for(srv: Any, runtime: Any, snapshot: dict[str, Any] | None, *, cid: str,
                  graph: Any, run_id: str, node_id: str, request_id: str, case_id: str,
                  case: dict[str, Any] | None = None, identity: dict[str, Any] | None = None) -> dict[str, Any]:
    stage = _node_stage(snapshot, node_id)
    case = case or {}
    value = {
        "conversation_id": cid,
        "cid": cid,
        "graph_hash": _graph_hash(graph or (snapshot or {}).get("locked_flow") or {}),
        "run_id": run_id,
        "node_id": node_id,
        "request_id": request_id,
        "case_id": case_id,
        "case_round": len(case.get("rounds") or []) if isinstance(case, dict) else 0,
        "epoch": case.get("epoch", stage.get("epoch", 0)) if isinstance(case, dict) else stage.get("epoch", 0),
        "input_revision": case.get("input_revision", stage.get("input_revision", "")) if isinstance(case, dict) else stage.get("input_revision", ""),
    }
    if isinstance(identity, dict):
        # Caller supplied identity can add stable host facts, but cannot
        # replace the scope fields above.
        for key, item in identity.items():
            if key not in value:
                value[key] = _clone(item)
    # Keep only the opaque authorization digest in the identity.  The generic
    # redactor deliberately drops keys containing ``authorization``; retaining
    # this one digest is required for the post-model CAS while exposing no
    # grant names, scopes, or credentials.
    authorization_hash = _auth_summary(snapshot, runtime).get("hash", "")
    value["authorization_hash"] = authorization_hash
    safe_value = _json_safe(value)
    safe_value["authorization_hash"] = authorization_hash
    return safe_value


def _scope_fingerprint(*, identity: dict[str, Any], questions: list[dict[str, Any]], context: dict[str, Any], roots: list[str]) -> str:
    return _digest({"identity": identity, "questions": questions, "context": context, "roots": roots})


def _failed_cache_matches(cached: Any, *, identity: dict[str, Any],
                          questions: list[dict[str, Any]], context: dict[str, Any],
                          roots: list[str]) -> bool:
    """Validate a failed fallback against the same frozen context.

    The shared core intentionally retries failed caches because its callers
    generally own an explicit retry button.  The desktop pending card must
    remain reconnect-safe, so this boundary treats a failure as reusable until
    ``refresh=True`` is explicitly requested.
    """

    if not isinstance(cached, dict) or cached.get("status") != "failed":
        return False
    if not cached.get("id") or not cached.get("version") or not isinstance(cached.get("questions"), list):
        return False
    if {item.get("name") for item in cached["questions"] if isinstance(item, dict)} != {
        item.get("name") for item in questions if isinstance(item, dict)
    }:
        return False
    try:
        context_hash = _core_context_hash(
            identity, questions, context, [Path(root) for root in roots]
        )
    except (OSError, TypeError, ValueError):
        return False
    return cached.get("context_hash") == context_hash


def _client_for(srv: Any, runtime: Any) -> Any:
    client = _runtime_value(runtime, "client") if runtime is not None else None
    if client is not None:
        return client
    return None


def _route_prepared_parameters(bundle, questions, work_dir, graph=None):
    """Bind technical processing parameters; never choose scope or authority."""
    from mewcode.experts.interaction_routing import derive_handling
    numeric = {'batch_size', 'pages_per_batch', 'batch_pages', 'max_retries', 'retry_delay'}
    temporary = {'scratch_dir', 'temporary_directory', 'temp_dir'}
    by_name = {item['name']: item for item in questions}
    branch_names = {edge['input_when']['input'] for edge in (graph or {}).get('edges', [])
                    if isinstance(edge.get('input_when'), dict) and edge['input_when'].get('input')}
    selections, answers, handling = {}, {}, {}
    for question in bundle.get('questions', []):
        name = question.get('name', '')
        source = by_name.get(name, {})
        technical = name in numeric | temporary and name not in branch_names and not source.get('required_when')
        route = derive_handling(purpose='decision' if technical else 'user_input')
        handling[name] = route
        if not technical:
            continue
        choices = [choice for choice in question.get('choices', []) if choice.get('kind') == 'value'
                   and not choice.get('requires_input')]
        choices.sort(key=lambda choice: not choice.get('recommended', False))
        for choice in choices:
            value = choice.get('value')
            if not isinstance(value, str) or not value.strip():
                continue
            if name in numeric:
                try:
                    import math
                    number = float(value)
                    if not math.isfinite(number) or number < 0 or (name != 'max_retries' and number == 0):
                        continue
                except ValueError:
                    continue
            else:
                try:
                    if not (Path(work_dir) / value).resolve().is_relative_to(Path(work_dir).resolve()):
                        continue
                except (TypeError, ValueError, OSError):
                    continue
            options = source.get('options') or []
            if options and value not in {option.get('value') for option in options}:
                continue
            selections[name] = choice['id']
            answers[name] = value
            route.update(handling_state='resolved', reason='主模型已选定处理参数，具体执行仍按原权限核验。')
            break
        else:
            route.update(handling_state='blocked', reason='尚未准备好可校验的内部参数，由主模型继续处理。')
    bundle['handling_by_question'] = handling
    bundle['system_selections'] = selections
    bundle['system_answers'] = answers
    return bundle


async def prepare_for_context(
    srv: Any,
    *,
    runtime: Any,
    snapshot: dict[str, Any] | None = None,
    questions: list[dict[str, Any]],
    node_id: str = "",
    identity: dict[str, Any] | None = None,
    cached: dict[str, Any] | None = None,
    refresh: bool = False,
    instruction: str = "",
) -> dict[str, Any]:
    """Prepare one exact context and return a bundle.

    This function intentionally does not mutate a live request or run store.
    Runtime waiters can call it while a future is suspended; the API endpoint
    performs its own post-model CAS before saving the result.
    """

    if not isinstance(questions, list):
        raise ValueError("问题合同必须是列表")
    cid = _text((identity or {}).get("conversation_id") or (identity or {}).get("cid") or _active_cid(srv))
    runtime_cid = _text(_runtime_value(runtime, "conversation_id") or
                        _runtime_value(runtime, "cid"))
    if runtime_cid and cid and runtime_cid != cid:
        raise ValueError("运行时对话与候选准备身份不一致")
    snapshot_copy = _clone(snapshot) if isinstance(snapshot, dict) else {}
    if not instruction:
        instruction = _text((_node_stage(snapshot_copy, node_id).get('pending_interaction') or {}).get('choice_instruction'))
    questions_copy = _normalise_questions(questions)
    questions_hash_before = _canonical(questions_copy)
    instruction_text = _text(instruction)
    if len(instruction_text) > _MAX_INSTRUCTION:
        raise ValueError("instruction 过长")
    graph = snapshot_copy.get("locked_flow") or snapshot_copy.get("spec") or {}
    run_id = _text((identity or {}).get("run_id") or snapshot_copy.get("run_id"))
    request_id = _text((identity or {}).get("request_id") or "request")
    case_id = _text((identity or {}).get("case_id"))
    case = _case_from_snapshot(snapshot_copy, case_id, node_id) if case_id else None
    effective_identity = _identity_for(
        srv, runtime, snapshot_copy, cid=cid, graph=graph, run_id=run_id,
        node_id=node_id, request_id=request_id, case_id=case_id, case=case, identity=identity,
    )
    context = _context_for(srv, runtime, snapshot_copy, questions_copy, graph=graph,
                           instruction=instruction_text, node_id=node_id)
    roots = _allowed_roots(srv, runtime, snapshot_copy, questions_copy, instruction_text)
    scope_hash = _scope_fingerprint(identity=effective_identity, questions=questions_copy,
                                     context=context, roots=roots)
    cache_key = (cid, run_id, node_id, request_id, case_id, scope_hash)
    state = _host_state(srv)
    lock = state["locks"].get(cache_key)
    if lock is None:
        lock = asyncio.Lock()
        state["locks"][cache_key] = lock
    async with lock:
        if not refresh:
            reusable = state["cache"].get(cache_key)
            if reusable is None:
                reusable = cached
            if _failed_cache_matches(
                    reusable, identity=effective_identity, questions=questions_copy,
                    context=context, roots=roots):
                reusable = _ensure_failed_retry_action(_clone(reusable))
                state["cache"][cache_key] = _clone(reusable)
                return _clone(reusable)
            # Core cache validation includes its own context hash, source
            # fingerprints and identity/version.  A failed bundle remains a
            # visible fallback but is deliberately retried on a future call.
            if isinstance(reusable, dict) and reusable.get("status") == "ready":
                candidate = await prepare_choices(
                    identity=effective_identity, questions=questions_copy, context=context,
                    complete=_no_model_complete, allowed_roots=roots, cached=reusable,
                )
                if candidate == reusable:
                    return _clone(candidate)
        client = _client_for(srv, runtime)
        if client is None:
            attempt_context = getattr(srv, "_orchestration_attempt_context", None)
            new_client = getattr(srv, "_new_orchestration_client", None)
            if callable(attempt_context) and callable(new_client):
                try:
                    route = attempt_context("")
                    effort = "" if str((route or {}).get("requested_effort") or "") == "auto" else str((route or {}).get("requested_effort") or "")
                    client = new_client(route, effort)
                except Exception:
                    client = None

        async def complete(system: str, user_text: str) -> str:
            if client is None:
                raise ValueError("当前编排没有可用模型客户端")
            return await collect_model_text(
                client, system=system, user_text=user_text,
                total_timeout=900, max_generated_chars=None,
            )

        bundle = await prepare_choices(
            identity=effective_identity,
            questions=questions_copy,
            context=context,
            complete=complete,
            allowed_roots=roots,
            cached=None if refresh else cached,
            refresh=refresh,
        )
        bundle = _ensure_failed_retry_action(bundle)
        bundle = _route_prepared_parameters(bundle, questions_copy, _workdir(srv, runtime), graph)
        current_cid = _active_cid(srv)
        if current_cid and current_cid != cid and runtime_cid != cid:
            raise _ScopeChanged("当前对话在准备期间已切换；未保存选项")
        # Detect an in-flight caller mutation before making the result
        # reusable.  This catches stale request/contract races even when the
        # host does not expose a durable store to this pure helper.
        if isinstance(identity, dict):
            for key, expected in effective_identity.items():
                if key in identity and key in {"conversation_id", "cid", "run_id", "node_id", "request_id", "case_id"}:
                    if _text(identity.get(key)) != _text(expected):
                        raise _ScopeChanged("请求身份在准备期间已变化；未保存选项")
        try:
            questions_hash_after = _canonical(_normalise_questions(questions))
        except (TypeError, ValueError) as exc:
            raise _ScopeChanged("问题合同在准备期间已变化；未保存选项") from exc
        if questions_hash_after != questions_hash_before:
            raise _ScopeChanged("问题合同在准备期间已变化；未保存选项")
        if isinstance(snapshot, dict) and _canonical(snapshot) != _canonical(snapshot_copy):
            raise _ScopeChanged("运行身份或授权在准备期间已变化；未保存选项")
        state["cache"][cache_key] = _clone(bundle)
        return _clone(bundle)


async def _no_model_complete(_system: str, _prompt: str) -> str:
    raise ValueError("没有模型客户端")


def _draft_state(srv: Any, cid: str, *, draft_id: str = "main") -> dict[str, Any] | None:
    reader = getattr(srv, "read_draft_state", None)
    if callable(reader):
        value = reader(_project_key(srv), cid, draft_id)
        return value if isinstance(value, dict) else None
    try:
        from mewcode.orchestration.draft_store import DraftStore
        return DraftStore.read_state(_project_key(srv), cid, draft_id)
    except (OSError, ValueError, TypeError):
        return None


def _find_pending(snapshot: dict[str, Any], request_id: str, node_id: str = "") -> tuple[str, dict[str, Any]]:
    states = snapshot.get("node_states") or {}
    if not isinstance(states, dict):
        return "", {}
    candidates = [(node_id, states.get(node_id))] if node_id else list(states.items())
    for key, stage in candidates:
        if not isinstance(stage, dict):
            continue
        pending = stage.get("pending_interaction")
        if isinstance(pending, dict) and (not request_id or _text(pending.get("request_id")) == request_id):
            return _text(key), pending
    return "", {}


def _run_owned(snapshot: dict[str, Any], srv: Any, cid: str, run_id: str) -> None:
    if not snapshot or _text(snapshot.get("run_id") or run_id) != run_id:
        raise ValueError("运行不存在")
    if _text(snapshot.get("conversation_id")) != cid:
        raise ValueError("运行不属于当前对话")
    project = snapshot.get("project") or snapshot.get("work_dir")
    if project and Path(project).resolve(strict=False) != _workdir(srv).resolve(strict=False):
        raise ValueError("运行不属于当前项目")


def _case_current(snapshot: dict[str, Any], case: dict[str, Any], node_id: str) -> None:
    if not case or case.get("node_id") != node_id:
        raise ValueError("案件不属于当前节点")
    if case.get('closed_by_user'):
        raise ValueError('本次修复已停止；旧准备结果不会重新打开案件')
    if case.get("status") not in {"awaiting_user", "reported", "open"}:
        raise ValueError("案件当前没有待答卡片")
    stage = _node_stage(snapshot, node_id)
    if stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}:
        raise ValueError('该节点已结束，当前检查点不再等待这张恢复卡')
    if stage.get("input_revision", "") != case.get("input_revision", ""):
        raise ValueError("案件输入已变化")
    epoch = stage.get("epoch")
    if node_id in (snapshot.get('node_ledgers') or {}):
        from mewcode.experts.node_ledger import NodeLedger
        epoch = NodeLedger(snapshot['node_ledgers']).view(node_id).epoch
    if epoch is not None and str(epoch) != str(case.get("epoch")):
        raise ValueError("案件代际已变化")
    expected_round = len(case.get("rounds") or [])
    pending = case.get("pending") if isinstance(case.get("pending"), dict) else {}
    for value in (pending.get("round"), (case.get("pending_choice_action") or {}).get("round")):
        if value is None:
            continue
        try:
            if int(value) != expected_round:
                raise ValueError("案件轮次已变化")
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc) == "案件轮次已变化":
                raise
            raise ValueError("案件轮次无效") from None


def _graph_record(srv: Any, cid: str, candidate_id: str, sequence: Any, *, draft_id: str = "main") -> tuple[dict[str, Any], dict[str, Any]]:
    state = _draft_state(srv, cid, draft_id=draft_id)
    candidate = (state or {}).get("candidate") if isinstance(state, dict) else None
    try:
        seq = int(sequence)
    except (TypeError, ValueError):
        raise ValueError("候选序列无效") from None
    if not isinstance(candidate, dict) or candidate.get("id") != candidate_id or candidate.get("sequence") != seq:
        raise ValueError("候选图已变化，请刷新后继续")
    if not isinstance(candidate.get("document"), dict):
        raise ValueError("当前候选图没有可核对的文档")
    return state or {}, candidate


def _persist_live_request(request: Any, source_questions: list[dict[str, Any]],
                          projected: list[dict[str, Any]], bundle: dict[str, Any], instruction: str = '') -> None:
    if request is None or isinstance(request, dict):
        if isinstance(request, dict):
            request["source_questions"] = _clone(source_questions)
            request["questions"] = _clone(projected)
            request["choice_bundle"] = _clone(bundle)
            request['choice_instruction'] = instruction
        return
    if callable(getattr(request, 'choice_state_signature', None)):
        request.choice_display_signature = request.choice_state_signature()
    request.choice_instruction = instruction
    try:
        request.source_questions = _clone(source_questions)
        request.questions = _clone(projected)
        request.choice_bundle = _clone(bundle)
    except Exception:
        pass


def _bundle_matches_record_context(
    srv: Any,
    record: dict[str, Any],
    *,
    runtime: Any,
    bundle: dict[str, Any],
    source_questions: list[dict[str, Any]],
    run_id: str,
    node_id: str,
    request_id: str,
    case_id: str,
    case: dict[str, Any] | None,
    instruction: str,
) -> bool:
    """Recompute the frozen core context before a durable CAS write."""

    cid = _text(record.get("conversation_id"))
    graph = record.get("locked_flow") or record.get("spec") or {}
    identity = _identity_for(
        srv, runtime, record, cid=cid, graph=graph, run_id=run_id,
        node_id=node_id, request_id=request_id, case_id=case_id, case=case,
    )
    context = _context_for(
        srv, runtime, record, source_questions, graph=graph,
        instruction=instruction, node_id=node_id,
    )
    roots = _allowed_roots(srv, runtime, record, source_questions, instruction)
    try:
        expected = _core_context_hash(identity, source_questions, context, [Path(root) for root in roots])
    except (OSError, TypeError, ValueError):
        return False
    return bundle.get("context_hash") == expected


def _mutate_run(srv: Any, run_id: str, callback: Callable[[dict[str, Any]], Any]) -> dict[str, Any] | None:
    store = _run_store(srv)
    if store is None:
        return None
    mutate = getattr(store, "mutate", None)
    if callable(mutate):
        try:
            value = mutate(run_id, callback, durable=True)
        except TypeError:
            value = mutate(run_id, callback)
        if inspect.isawaitable(value):
            raise ValueError("运行保存不得异步跨越候选准备边界")
        return value if isinstance(value, dict) else None
    loaded = _load_run(srv, run_id)
    if loaded is not None:
        callback(loaded)
        return loaded
    return None


def _persist_run_bundle(srv: Any, *, run_id: str, node_id: str, request_id: str,
                        case_id: str, source_questions: list[dict[str, Any]],
                        projected: list[dict[str, Any]], bundle: dict[str, Any],
                        expected_scope: str, expected_auth_hash: str = "", runtime: Any = None,
                        instruction: str = "") -> dict[str, Any] | None:
    def update(record: dict[str, Any]) -> dict[str, Any]:
        if _text(record.get("conversation_id")) != _active_cid(srv):
            raise _ScopeChanged("运行所属对话已变化；未保存选项")
        if expected_auth_hash and _auth_summary(record, _runtime_for(srv, _active_cid(srv))).get("hash") != expected_auth_hash:
            raise _ScopeChanged("授权摘要已变化；未保存选项")
        stage = _node_stage(record, node_id)
        pending = stage.get("pending_interaction")
        if not isinstance(pending, dict) or _text(pending.get("request_id")) != request_id:
            raise _ScopeChanged("待答请求已变化；未保存选项")
        saved_source = pending.get("source_questions")
        if saved_source not in (None, [], ""):
            original = _normalise_questions(saved_source)
            if _canonical(original) != _canonical(source_questions):
                raise _ScopeChanged("问题合同已变化；未保存选项")
        if not _bundle_matches_record_context(
                srv, record, runtime=runtime or _runtime_for(srv, _active_cid(srv)),
                bundle=bundle, source_questions=source_questions, run_id=run_id,
                node_id=node_id, request_id=request_id, case_id=case_id,
                case=_case_from_snapshot(record, case_id, node_id) if case_id else None,
                instruction=instruction):
            raise _ScopeChanged("运行上下文已变化；未保存选项")
        if expected_scope and _scope_fingerprint(
            identity=_identity_for(srv, None, record, cid=_active_cid(srv), graph=record.get("locked_flow") or {},
                                    run_id=run_id, node_id=node_id, request_id=request_id, case_id=case_id,
                                    case=_case_from_snapshot(record, case_id, node_id)),
            questions=_normalise_questions(pending.get("source_questions") or pending.get("questions") or source_questions),
            context=_context_for(srv, None, record, source_questions, graph=record.get("locked_flow") or {}, node_id=node_id),
            roots=_allowed_roots(srv, None, record, source_questions, ""),
        ) != expected_scope:
            # Hosts that add transient metadata may not reproduce the exact
            # original hash.  The request id check above remains mandatory;
            # only reject a clearly changed source contract.
            original = _normalise_questions(pending.get("source_questions") or source_questions)
            if _canonical(original) != _canonical(source_questions):
                raise _ScopeChanged("问题合同已变化；未保存选项")
        pending["source_questions"] = _clone(source_questions)
        pending['choice_instruction'] = instruction
        pending["questions"] = _clone(projected)
        pending["choice_bundle"] = _clone(bundle)
        pending["choice_cache"] = {"bundle_id": bundle.get("id"), "version": bundle.get("version"),
                                    "context_hash": bundle.get("context_hash")}
        if case_id:
            case = _case_from_snapshot(record, case_id, node_id)
            if case is None:
                raise _ScopeChanged("案件已变化；未保存选项")
            _case_current(record, case, node_id)
            case_pending = case.setdefault("pending", {})
            case_pending["source_questions"] = _clone(source_questions)
            case_pending["choice_instruction"] = instruction
            case_pending["questions"] = _clone(projected)
            case_pending["choice_bundle"] = _clone(bundle)
            case_pending["choice_cache"] = copy.deepcopy(pending["choice_cache"])
            stage["recovery_case"] = _clone(case)
        return record

    return _mutate_run(srv, run_id, update)


def _persist_case_bundle(srv: Any, *, run_id: str, node_id: str, case_id: str,
                         request_id: str, action: str, source_questions: list[dict[str, Any]],
                         projected: list[dict[str, Any]], bundle: dict[str, Any],
                         expected_auth_hash: str = "", runtime: Any = None,
                         conversation_id: str = "", instruction: str = "",
                         expected_case_stamp: str = "") -> dict[str, Any] | None:
    """CAS-save a re-prepared recovery card without starting its case."""

    def update(record: dict[str, Any]) -> dict[str, Any]:
        owner_cid = conversation_id or _scope_cid(srv, runtime)
        if _text(record.get("conversation_id")) != owner_cid:
            raise _ScopeChanged("运行所属对话已变化；未保存恢复选项")
        if expected_auth_hash and _auth_summary(
                record, runtime or _runtime_for(srv, owner_cid)).get("hash") != expected_auth_hash:
            raise _ScopeChanged("授权摘要已变化；未保存恢复选项")
        case = _case_from_snapshot(record, case_id, node_id)
        _case_current(record, case or {}, node_id)
        if case is None:
            raise _ScopeChanged("案件已变化；未保存恢复选项")
        if case.get('status') not in {'reported', 'awaiting_user'} or not case.get('pending'):
            raise _ScopeChanged('问题已回答或转入系统处理；旧候选未重新挂起')
        if expected_case_stamp and _case_choice_stamp(case) != expected_case_stamp:
            raise _ScopeChanged('恢复卡在准备期间已变化；旧结果未覆盖你的选择')
        selected = case.get("pending_choice_action") or {}
        if action and (selected.get("action") != action or selected.get("case_id") != case_id):
            raise _ScopeChanged("案件动作已变化；未保存恢复选项")
        if not action and selected and selected.get("case_id") not in {None, case_id}:
            raise _ScopeChanged("案件动作已变化；未保存恢复选项")
        if request_id and _text((case.get("pending") or {}).get("request_id")) not in {"", request_id}:
            raise _ScopeChanged("案件问题已变化；未保存恢复选项")
        pending = case.setdefault("pending", {})
        saved_source = pending.get("source_questions")
        if saved_source not in (None, [], ""):
            if _canonical(_normalise_questions(saved_source)) != _canonical(source_questions):
                raise _ScopeChanged("案件问题合同已变化；未保存恢复选项")
        if not _bundle_matches_record_context(
                srv, record, runtime=runtime or _runtime_for(srv, owner_cid),
                bundle=bundle, source_questions=source_questions, run_id=run_id,
                node_id=node_id, request_id=request_id, case_id=case_id,
                case=case, instruction=instruction):
            raise _ScopeChanged("案件上下文已变化；未保存恢复选项")
        pending["source_questions"] = _clone(source_questions)
        pending["choice_instruction"] = instruction
        pending["questions"] = _clone(projected)
        pending["choice_bundle"] = _clone(bundle)
        pending["choice_cache"] = {"bundle_id": bundle.get("id"), "version": bundle.get("version"),
                                    "context_hash": bundle.get("context_hash")}
        if action:
            case.pop("pending_choice_action", None)
        stage = record.setdefault("node_states", {}).setdefault(node_id, {})
        stage["recovery_case"] = _clone(case)
        if action:
            stage.pop("pending_choice_action", None)
        stage["status"] = "paused"
        # Save the case ledger fact when the real host exposes it.  This is a
        # host maintenance update, not a round start or a business answer.
        try:
            case.setdefault("events", [])
            case.setdefault("rounds", [])
            from mewcode.experts.recovery_case import save_case
            save_case(record, case)
        except (ImportError, KeyError, TypeError, ValueError):
            pass
        return record

    return _mutate_run(srv, run_id, update)


async def refresh_case_choices(
    srv: Any,
    *,
    runtime: Any,
    snapshot: dict[str, Any],
    case_id: str,
    instruction: str = "",
    action: str = "research",
) -> dict[str, Any]:
    """Re-prepare a selected recovery-card action and keep the card waiting.

    ``research``, ``configure`` and ``retry_prepare`` only rebuild the visible
    choice bundle.  ``defer`` deliberately does nothing.  ``revise`` returns a
    maintenance handoff marker for the existing contract-fix UI; it does not
    silently rewrite a recovery card into a different business action.
    """

    if action not in {"", "research", "configure", "retry_prepare", "revise", "defer"}:
        raise ValueError("恢复动作不受支持")
    if not isinstance(snapshot, dict):
        raise ValueError("运行快照无效")
    cid = _scope_cid(srv, runtime)
    if _text(snapshot.get("conversation_id")) != cid:
        raise ValueError("运行不属于当前对话")
    stage_candidates = snapshot.get("node_states") or {}
    case = None
    node_id = ""
    if isinstance(stage_candidates, dict):
        for key, stage in stage_candidates.items():
            candidate_case = stage.get("recovery_case") if isinstance(stage, dict) else None
            if isinstance(candidate_case, dict) and candidate_case.get("case_id") == case_id:
                case, node_id = candidate_case, _text(key)
                break
    if case is None:
        raise ValueError("案件不存在")
    _case_current(snapshot, case, node_id)
    selected = case.get("pending_choice_action") or {}
    selected_action = _text(selected.get("action"))
    if action == "defer":
        return _clone(snapshot)
    if action and selected_action and selected_action != action:
        raise ValueError("案件动作已变化，请重新打开当前卡片")
    prompt = (_text(instruction) or _text(selected.get("input"))
              or _text((case.get("pending") or {}).get("choice_instruction")))
    if action == "revise":
        maintenance = {
            "kind": "contract_maintenance",
            "status": "needs_review",
            "run_id": _text(snapshot.get("run_id")),
            "conversation_id": cid,
            "case_id": case_id,
            "node_id": node_id,
            "root_cause": _text((case.get("report") or {}).get("root_cause")),
            "open_fix_entry": "/api/orchestration/maintenance/open",
            "entry": {
                "method": "POST",
                "path": "/api/orchestration/maintenance/open",
                "body": {
                    "conversation_id": cid,
                    "project": str(_workdir(srv, runtime)),
                    "mode": "fix",
                    "request": ("案件 " + case_id + " 的流程合同问题（节点 " + node_id
                                + "，根因 " + _text((case.get("report") or {}).get("root_cause")) + "）"
                                + ("\n用户明确要求：" + _redact_text(prompt) if prompt else "")),
                    "node_ids": [node_id],
                    "fresh": True,
                },
            },
            "message": "请在原有维护入口调查并预览流程合同修订；本案件仍保留在待答状态。",
        }

        def mark(record: dict[str, Any]) -> dict[str, Any]:
            if _text(record.get("conversation_id")) != cid:
                raise _ScopeChanged("运行所属对话已变化；未创建维护入口")
            current = _case_from_snapshot(record, case_id, node_id)
            _case_current(record, current or {}, node_id)
            if current is None:
                raise _ScopeChanged("案件已变化；未创建维护入口")
            current.setdefault("pending", {})["maintenance"] = _clone(maintenance)
            current.pop("pending_choice_action", None)
            stage = record.setdefault("node_states", {}).setdefault(node_id, {})
            stage["recovery_case"] = _clone(current)
            stage.pop("pending_choice_action", None)
            stage["maintenance_choice"] = _clone(maintenance)
            try:
                current.setdefault("events", [])
                current.setdefault("rounds", [])
                from mewcode.experts.recovery_case import save_case
                save_case(record, current)
            except (ImportError, KeyError, TypeError, ValueError):
                pass
            return record

        updated = _mutate_run(srv, _text(snapshot.get("run_id")), mark)
        result = _clone(updated or snapshot)
        result["maintenance"] = maintenance
        return result

    questions, pending = _case_questions(case, node_id)
    questions = _normalise_questions(questions)
    identity = _identity_for(
        srv, runtime, snapshot, cid=cid, graph=snapshot.get("locked_flow") or snapshot.get("spec") or {},
        run_id=_text(snapshot.get("run_id")), node_id=node_id,
        request_id=_text(pending.get("request_id") or selected.get("request_id") or case_id),
        case_id=case_id, case=case,
    )
    bundle = await prepare_for_context(
        srv, runtime=runtime, snapshot=snapshot, questions=questions, node_id=node_id,
        identity=identity, cached=pending.get("choice_bundle"), refresh=bool(action),
        instruction=prompt,
    )
    projected = project_questions(questions, bundle)
    updated = _persist_case_bundle(
        srv, run_id=_text(snapshot.get("run_id")), node_id=node_id, case_id=case_id,
        request_id=identity['request_id'], action=action,
        source_questions=questions, projected=projected, bundle=bundle,
        expected_auth_hash=identity.get("authorization_hash", ""),
        runtime=runtime, conversation_id=cid, instruction=prompt,
        expected_case_stamp=_case_choice_stamp(case),
    )
    return _clone(updated or snapshot)


def _cache_graph(srv: Any, cid: str, candidate_id: str, sequence: Any,
                 bundle: dict[str, Any], questions: list[dict[str, Any]],
                 *, candidate: dict[str, Any] | None = None, graph: Any = None) -> None:
    candidate = candidate if isinstance(candidate, dict) else {}
    base_hash = _text(candidate.get("base_graph_hash")) or _graph_hash(
        candidate.get("base_snapshot") or graph or {}
    )
    _host_state(srv)["graph"][(cid, candidate_id, int(sequence))] = {
        "bundle": _clone(bundle), "questions": _clone(questions), "base_hash": base_hash,
    }


def _cache_startup(srv: Any, cid: str, spec: dict[str, Any], bundle: dict[str, Any], questions: list[dict[str, Any]]) -> None:
    spec_hash = _graph_hash(spec)
    _host_state(srv)["startup"][(cid, spec_hash)] = {
        "spec_hash": spec_hash, "bundle": _clone(bundle), "questions": _clone(questions),
        "spec": _clone(spec), "task_id": (spec.get('meta', {}).get('task_design') or {}).get('task_id', ''),
    }
    from mewcode.orchestration.workbench_events import changed
    changed('runtime', conversation_id=cid)


def lookup_startup_bundle(srv: Any, cid: str, bundle_id: str, version: str, spec: Any) -> dict[str, Any]:
    """Return a startup bundle only when conversation and spec scope match."""

    current = _active_cid(srv)
    if not cid or cid != current:
        raise ValueError("当前对话已切换；启动选项包已过期")
    owned = _owned_spec(srv, spec, cid)
    spec_value = _spec_dict(owned)
    record = _host_state(srv)["startup"].get((cid, _graph_hash(spec_value)))
    bundle = record.get("bundle") if isinstance(record, dict) else None
    if not isinstance(bundle, dict) or bundle.get("id") != bundle_id or bundle.get("version") != version:
        raise ValueError("启动选项包不存在或已过期")
    return _clone(bundle)


def resolve_graph_selections(
    srv: Any,
    selections: Any,
    bundle_id: str = "",
    version: str = "",
    *,
    conversation_id: str = "",
    candidate_id: str = "",
    sequence: Any = None,
    bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve graph presentation ids into host-bound canonical selections.

    The returned ``selections`` list is safe to pass to the legacy graph
    chooser after the caller supplies the host-only ``prepared_options`` to
    that chooser.  AI choice ids never cross that boundary.  A candidate id
    and sequence therefore revalidate the cached bundle before any value is
    returned.
    """

    cid = conversation_id or _active_cid(srv)
    if cid != _active_cid(srv):
        raise ValueError("当前对话已切换；构图选项已过期")
    graph_record = None
    if candidate_id:
        state, candidate = _graph_record(srv, cid, candidate_id, sequence)
        try:
            key = (cid, candidate_id, int(sequence))
        except (TypeError, ValueError):
            raise ValueError("候选图序列无效") from None
        graph_record = _host_state(srv)["graph"].get(key)
        if not isinstance(graph_record, dict):
            raise ValueError("构图选项包不存在或已过期")
        current_base_hash = _text(candidate.get("base_graph_hash")) or _graph_hash(
            candidate.get("base_snapshot") or {}
        )
        if _text(graph_record.get("base_hash")) != current_base_hash:
            raise ValueError("候选图基线已变化，请刷新后继续")
        if bundle is None:
            bundle = graph_record.get("bundle")
        elif bundle.get("id") != (graph_record.get("bundle") or {}).get("id"):
            raise ValueError("构图选项包已过期")
    elif bundle is None:
        records = [value for key, value in _host_state(srv)["startup"].items() if key[0] == cid]
        bundle = next((value.get("bundle") for value in records if isinstance(value, dict)
                       and value.get("bundle", {}).get("id") == bundle_id), None)
    if not isinstance(bundle, dict):
        raise ValueError("构图选项包不存在或已过期")
    if bundle_id or version:
        resolve_answers(bundle, {}, bundle_id=bundle_id, version=version)
    if isinstance(selections, dict):
        answers = _clone(selections)
    elif isinstance(selections, list):
        answers = {}
        for item in selections:
            if not isinstance(item, dict) or not isinstance(item.get("question_id"), str):
                raise ValueError("构图选择格式无效")
            answers[item["question_id"]] = item.get("value", item.get("id"))
    else:
        raise ValueError("构图选择必须是对象或列表")
    resolved = resolve_answers(bundle, answers, bundle_id=bundle_id, version=version)
    if resolved.get("actions"):
        raise ValueError("构图业务选择不能提交动作")
    values = resolved.get("values") or {}
    if any(isinstance(value, list) for value in values.values()):
        raise ValueError("构图业务选择必须是单值")
    prepared_options: dict[str, list[dict[str, Any]]] = {}
    for question in bundle.get("questions") or []:
        if not isinstance(question, dict) or not isinstance(question.get("name"), str):
            continue
        options = []
        for choice in question.get("choices") or []:
            if not isinstance(choice, dict) or choice.get("kind") == "action":
                continue
            value = choice.get("value")
            if not isinstance(value, str):
                # The legacy graph chooser accepts scalar text values only.
                continue
            row = {"value": value, "label": _text(choice.get("label") or value)}
            for key in ("description", "recommended"):
                if key in choice:
                    row[key] = _clone(choice[key])
            options.append(row)
        if options:
            prepared_options[question["name"]] = options
    for name, value in values.items():
        if isinstance(value, str) and not any(row.get('value') == value for row in prepared_options.get(name, [])):
            # resolve_answers only admits an explicit custom envelope when
            # this frozen question exposes Other. Keep the user's own text.
            prepared_options.setdefault(name, []).append({'value': value, 'label': value[:100],
                                                          'description': '你提供的其他方案'})
    selections = [
        {"question_id": name, "value": value}
        for name, value in values.items()
        if isinstance(value, str)
    ]
    return {"selections": selections, "prepared_options": prepared_options}


async def _read_body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception as exc:
        raise ValueError("请求必须是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise ValueError("请求必须是 JSON 对象")
    return value


def _body_text(body: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = body.get(key, "")
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是文本")
    value = value.strip()
    if required and not value:
        raise ValueError(f"缺少 {key}")
    if "\x00" in value or len(value) > 4000:
        raise ValueError(f"{key} 无效")
    return value


def _current_run_scope(srv: Any, cid: str, run_id: str, request_id: str, node_id: str, case_id: str,
                       supplied_spec: Any) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None, Any]:
    snapshot = _load_run(srv, run_id)
    if snapshot is None:
        raise ValueError("运行不存在或已结束")
    _run_owned(snapshot, srv, cid, run_id)
    runtime = _runtime_for(srv, cid)
    live, _ = _pending_request(runtime, request_id)
    found_node, pending = _find_pending(snapshot, request_id, node_id)
    node_id = node_id or found_node
    case = _case_from_snapshot(snapshot, case_id, node_id) if case_id else None
    saved_case_pending = case.get("pending") if isinstance(case, dict) else None
    if live is None and not pending and not (case_id and isinstance(saved_case_pending, dict) and saved_case_pending):
        raise ValueError("待答请求不存在或已过期")
    if pending and request_id and _text(pending.get("request_id")) != request_id:
        raise ValueError("待答请求已变化")
    if live is not None and not pending:
        source_questions = _request_questions(live)
    else:
        source_questions = _normalise_questions(pending.get("source_questions") or pending.get("questions") or [])
    if case_id:
        _case_current(snapshot, case or {}, node_id)
        case_questions, case_pending = _case_questions(case, node_id)
        if case_questions:
            source_questions = case_questions
            pending = case_pending
    if not source_questions:
        stage = _node_stage(snapshot, node_id)
        source_questions = _normalise_questions(stage.get("stage_input_questions") or [])
    if not source_questions:
        raise ValueError("当前请求没有可准备的问题合同")
    graph = snapshot.get("locked_flow") or snapshot.get("spec") or {}
    if supplied_spec not in (None, {}, ""):
        supplied = _owned_spec(srv, supplied_spec, cid)
        supplied_value = _spec_dict(supplied)
        if graph and _graph_hash(_normalised_spec_dict(graph)) != _graph_hash(supplied_value):
            raise ValueError("请求方案与当前运行图不一致")
        graph = supplied_value
    return snapshot, source_questions, case, (runtime, graph, live, pending, node_id)


def install(app: Any, srv: Any) -> None:
    """Register the desktop-only preparation route on the host app."""

    @app.post(_ROUTE)
    async def interaction_choices_prepare(request: Request):
        try:
            body = await _read_body(request)
            cid = _body_text(body, "conversation_id", required=True)
            if cid != _active_cid(srv):
                raise ValueError("当前对话已切换，请重新打开编排")
            run_id = _body_text(body, "run_id")
            node_id = _body_text(body, "node_id")
            request_id = _body_text(body, "request_id")
            case_id = _body_text(body, "case_id")
            candidate_id = _body_text(body, "candidate_id")
            instruction = _body_text(body, "instruction")
            action_input = _body_text(body, "action_input")
            refresh = body.get("refresh", False)
            if type(refresh) is not bool:
                raise ValueError("refresh 必须是布尔值")
            supplied_spec = body.get("spec")
            if candidate_id and (run_id or case_id):
                raise ValueError("候选图请求不能同时携带运行或案件身份")
            if case_id and not run_id:
                raise ValueError("案件准备必须提供 run_id")
            runtime = _runtime_for(srv, cid)
            live_question, live_kind = _pending_request(runtime, request_id) if request_id else (None, '')
            planning_request = not run_id and not case_id and not candidate_id and live_question is not None
            startup = not run_id and not case_id and not candidate_id and not planning_request
            graph: Any = None
            candidate: dict[str, Any] | None = None
            snapshot: dict[str, Any] = {}
            case: dict[str, Any] | None = None
            live = None
            pending: dict[str, Any] = {}
            if planning_request:
                live = live_question
                source_questions = _request_questions(live)
                graph = _normalised_spec_dict(_current_spec(srv, cid) or {})
                snapshot = {'locked_flow': graph}
            elif startup:
                if supplied_spec in (None, {}, ""):
                    raise ValueError("启动准备必须提供 spec")
                owned = _owned_spec(srv, supplied_spec, cid)
                graph = _spec_dict(owned)
                values = {}
                if isinstance(body.get("run_inputs"), dict):
                    values = _clone(body["run_inputs"])
                elif isinstance(runtime, dict) and isinstance(runtime.get("orchestration_run_inputs"), dict):
                    values = _clone(runtime["orchestration_run_inputs"])
                elif runtime is not None and isinstance(getattr(runtime, "orchestration_run_inputs", None), dict):
                    values = _clone(runtime.orchestration_run_inputs)
                source_questions, normalized = _contract_questions_from_spec(owned, values)
                snapshot = {"run_inputs": normalized, "locked_flow": graph}
                request_id = request_id or "startup"
            elif candidate_id:
                state, candidate = _graph_record(srv, cid, candidate_id, body.get("sequence"))
                snapshot = state
                graph = _normalised_spec_dict(
                    candidate.get("base_snapshot") or _current_spec(srv, cid, supplied_spec) or {}
                )
                if graph:
                    # DraftStore snapshots may contain only candidate metadata;
                    # make the exact base graph available to the frozen model
                    # context without writing it back to the draft.
                    snapshot = _clone(snapshot)
                    snapshot["locked_flow"] = _clone(graph)
                source_questions = _graph_questions(candidate.get("document") or {})
                if supplied_spec not in (None, {}, "") and graph and _graph_hash(graph) != _graph_hash(_spec_dict(_owned_spec(srv, supplied_spec, cid))):
                    raise ValueError("构图方案已变化，请刷新候选图")
                if not source_questions:
                    # An empty graph question set is a valid read-only bundle.
                    source_questions = []
            else:
                if not run_id or not request_id:
                    raise ValueError("运行准备必须提供 run_id 和 request_id")
                snapshot, source_questions, case, extra = _current_run_scope(
                    srv, cid, run_id, request_id, node_id, case_id, supplied_spec,
                )
                runtime, graph, live, pending, node_id = extra
            source_questions = _normalise_questions(source_questions)
            instruction_text = instruction
            if action_input:
                instruction_text = (instruction_text + "\n" if instruction_text else "") + "用户补充：" + action_input
            if not instruction_text:
                instruction_text = (_text(getattr(live, 'choice_instruction', '')) if live else '') or _text(pending.get('choice_instruction'))
            identity = _identity_for(
                srv, runtime, snapshot, cid=cid, graph=graph, run_id=run_id,
                node_id=node_id, request_id=request_id or "startup", case_id=case_id, case=case,
            )
            cached = None
            state = _host_state(srv)
            if startup:
                cached_record = state["startup"].get((cid, _graph_hash(graph)))
                cached = (cached_record or {}).get("bundle") if isinstance(cached_record, dict) else None
            elif candidate:
                try:
                    cached = (state["graph"].get((cid, candidate_id, int(body.get("sequence")))) or {}).get("bundle")
                except (TypeError, ValueError):
                    cached = None
            elif planning_request:
                cached = getattr(live, 'choice_bundle', None)
            else:
                cached = pending.get("choice_bundle") if isinstance(pending, dict) else None
            bundle = await prepare_for_context(
                srv, runtime=runtime, snapshot=snapshot, questions=source_questions,
                node_id=node_id, identity=identity, cached=cached,
                refresh=refresh, instruction=instruction_text,
            )
            projected = project_questions(source_questions, bundle)
            if cid != _active_cid(srv):
                raise _ScopeChanged('对话在准备期间已切换，未覆盖新问题')
            # Persist only after a fresh host-side CAS check.  Graph/startup
            # preparation stores a host-local lookup record; it never edits a
            # candidate document or records a business decision.
            if startup:
                _cache_startup(srv, cid, graph, bundle, source_questions)
            elif candidate:
                _, candidate_after = _graph_record(
                    srv, cid, candidate_id, body.get("sequence")
                )
                if (_digest(candidate_after.get("document") or {}) != _digest(candidate.get("document") or {})
                        or _text(candidate_after.get("base_graph_hash")) != _text(candidate.get("base_graph_hash"))):
                    raise _ScopeChanged("候选图在准备期间已变化；未保存选项")
                _cache_graph(srv, cid, candidate_id, body.get("sequence"), bundle, source_questions,
                             candidate=candidate, graph=graph)
            elif planning_request:
                current_live, _ = _pending_request(runtime, request_id)
                if current_live is not live or _canonical(_request_questions(current_live)) != _canonical(source_questions):
                    raise _ScopeChanged('当前提问已变化，未覆盖新问题')
                if _graph_hash(_normalised_spec_dict(_current_spec(srv, cid) or {})) != _graph_hash(graph):
                    raise _ScopeChanged('提问所依据的编排图已变化，未覆盖新问题')
                _persist_live_request(live, source_questions, projected, bundle, instruction_text)
            elif case_id:
                _persist_case_bundle(
                    srv, run_id=run_id, node_id=node_id, case_id=case_id,
                    request_id=request_id, action='', source_questions=source_questions,
                    projected=projected, bundle=bundle,
                    expected_auth_hash=identity.get('authorization_hash', ''),
                    runtime=runtime, conversation_id=cid, instruction=instruction_text,
                    expected_case_stamp=_case_choice_stamp(case),
                )
            else:
                _persist_run_bundle(
                    srv, run_id=run_id, node_id=node_id, request_id=request_id, case_id=case_id,
                    source_questions=source_questions, projected=projected, bundle=bundle,
                    expected_scope="", expected_auth_hash=identity.get("authorization_hash", ""),
                    runtime=runtime, instruction=instruction_text,
                )
                _persist_live_request(live, source_questions, projected, bundle, instruction_text)
            return {
                "choice_bundle": bundle,
                "questions": projected,
                "conversation_id": cid,
                "run_id": run_id,
                "node_id": node_id,
                "request_id": request_id,
                "case_id": case_id,
                "candidate_id": candidate_id,
                "sequence": body.get("sequence"),
            }
        except _ScopeChanged as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get(_ROUTE)
    async def interaction_choices_read(request: Request):
        """Read a host-cached bundle without ever invoking a model."""

        try:
            params = request.query_params
            cid = _text(params.get("conversation_id"))
            if cid != _active_cid(srv):
                raise ValueError("当前对话已切换")
            bundle_id = _text(params.get("bundle_id") or params.get("choice_bundle_id"))
            version = _text(params.get("version") or params.get("choice_version"))
            matches = []
            for value in _host_state(srv)["startup"].values():
                if isinstance(value, dict) and value.get("bundle", {}).get("id") == bundle_id:
                    matches.append(value.get("bundle"))
            for value in _host_state(srv)["graph"].values():
                if isinstance(value, dict) and value.get("bundle", {}).get("id") == bundle_id:
                    matches.append(value.get("bundle"))
            bundle = next((item for item in matches if isinstance(item, dict) and item.get("version") == version), None)
            if not bundle:
                raise ValueError("选项包不存在或已过期")
            return {"choice_bundle": _clone(bundle), "conversation_id": cid}
        except (ValueError, OSError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)


__all__ = [
    "install",
    "prepare_for_context",
    "refresh_case_choices",
    "lookup_startup_bundle",
    "resolve_graph_selections",
]
