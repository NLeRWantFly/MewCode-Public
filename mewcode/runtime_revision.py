"""Compare loaded diagnostic code with disk without reloading or executing it.

These are freshness observations for the listed symbols, never permissions or
proof that an entire installation has been validated.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
import sys
from types import CodeType


_SYMBOLS = {
    "mewcode.agent": ("Agent._host_task_review_scope", "Agent._uses_native_commands"),
    "mewcode.permissions.checker": ("PermissionChecker.uses_native_commands",),
    "mewcode.tools.runtime_doctor": ("RuntimeDoctor.execute",),
    "mewcode.permissions.reviewer": ("SafetyReviewer._conversation_context", "SafetyReviewer.review"),
    "mewcode.orchestration.graph_lint": ("_positive_match", "lint_graph"),
    "mewcode.kernel.preflight": ("inspect",),
    "mewcode.kernel.escalation": ("decide",),
    "mewcode.orchestration.graph_inspection": ("diagnose",),
    "mewcode.orchestration.graph_readiness": ("readiness",),
    "mewcode.orchestration.workflow_draft": ("check_entry_refs", "project_graph"),
    "mewcode.orchestration.design_health": ("design_health",),
    "mewcode.orchestration.draft_sources": ("read_sources",),
    "mewcode.orchestration.history_scan": ("history_lines",),
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _code_value(value):
    if isinstance(value, CodeType):
        # Exclude source locations and interpreter-adaptive instructions. A
        # comment or line-number change is not a stale implementation.
        return {"code": value.co_code.hex(), "constants": [_code_value(v) for v in value.co_consts],
            "names": value.co_names, "vars": value.co_varnames, "free": value.co_freevars,
            "cells": value.co_cellvars, "args": value.co_argcount, "posonly": value.co_posonlyargcount,
            "kwonly": value.co_kwonlyargcount, "flags": value.co_flags,
            "exceptions": getattr(value, "co_exceptiontable", b"").hex()}
    if isinstance(value, tuple):
        return ["tuple", [_code_value(v) for v in value]]
    if isinstance(value, frozenset):
        return ["frozenset", sorted((_code_value(v) for v in value), key=repr)]
    return [type(value).__name__, repr(value)]


@lru_cache(maxsize=32)
def _disk_codes(path, modified_ns, size):
    del modified_ns, size  # Cache invalidation only, never evidence of equality.
    source = Path(path).read_bytes()
    if len(source) > 2 * 1024 * 1024:
        raise ValueError("source exceeds diagnostic limit")
    root = compile(source, path, "exec", dont_inherit=True, optimize=sys.flags.optimize)
    result = {}
    def visit(code):
        result[code.co_qualname] = _digest(_code_value(code))
        for value in code.co_consts:
            if isinstance(value, CodeType):
                visit(value)
    visit(root)
    return result


def inspect_symbol(function):
    """Internal helper; callers choose a host-owned callable, never a user path."""
    code = getattr(function, "__code__", None)
    if code is None:
        return {"status": "unavailable", "reason": "not_a_python_function"}
    loaded = _digest(_code_value(code))
    try:
        path = Path(code.co_filename)
        stat = path.stat()
        if stat.st_size > 2 * 1024 * 1024:
            raise ValueError("source exceeds diagnostic limit")
        disk = _disk_codes(str(path), stat.st_mtime_ns, stat.st_size).get(code.co_qualname)
        if disk is None:
            return {"status": "changed", "loaded": loaded, "reason": "symbol_missing_on_disk"}
        return {"status": "current" if loaded == disk else "changed", "loaded": loaded, "disk": disk}
    except (OSError, SyntaxError, ValueError, TypeError):
        return {"status": "unavailable", "loaded": loaded, "reason": "disk_code_unverifiable"}


def runtime_revision():
    rows = {}
    for module_name, names in _SYMBOLS.items():
        module = sys.modules.get(module_name)
        for name in names:
            value = module
            for part in name.split("."):
                value = getattr(value, part, None)
            # Do not import a new implementation just to make an old process
            # appear current, or inspect test/plugin replacements as host code.
            if value is None or getattr(value, "__module__", "") != module_name:
                rows[module_name + "." + name] = {"status": "unavailable", "reason": "symbol_not_loaded"}
            else:
                rows[module_name + "." + name] = inspect_symbol(value)
    changed = [name for name, row in rows.items() if row["status"] == "changed"]
    unknown = [name for name, row in rows.items() if row["status"] == "unavailable"]
    return {"schema": "mewcode.runtime-revision.v1", "fingerprint": _digest(rows),
        "status": "restart_required" if changed else "partially_verified" if unknown else "current",
        "changed_symbols": changed, "unverified_symbols": unknown, "components": rows,
        "scope": "Listed loaded functions only; no reload, restart, command or permission change."}
