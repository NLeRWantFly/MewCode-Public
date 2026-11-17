"""Read-only, bounded build diagnostics, including pre-diagnostic checkpoints."""
from __future__ import annotations

import re

from mewcode.permissions.decision_envelope import redact_value


def _failure(record, origin):
    if not isinstance(record, dict):
        return None
    failure = record.get("failure")
    if isinstance(failure, dict) and failure:
        result = {key: failure[key] for key in
                  ("phase", "stage", "batch", "category", "exception_type", "http_status", "retryable", "route")
                  if key in failure}
        result["summary"] = str(failure.get("summary") or "")[:1200]
        return {**redact_value(result), "origin": origin, "legacy": False}
    error = record.get("error")
    if not error:
        return None
    # Old review checkpoints stored only the exception class. Do not turn
    # arbitrary error text into either a credential leak or retry authority.
    exception = error if isinstance(error, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,100}", error) else "UnknownError"
    return {"origin": origin, "legacy": True, "exception_type": exception, "retryable": None,
            "summary": f"覆盖审查失败（{exception}）；旧记录未保存具体异常文本。"}


def build_diagnostics(state):
    candidate = state.get("candidate") or {}
    build = candidate.get("autobuild") or {}
    if not build:
        return None
    result = {"candidate_id": candidate.get("id"), "candidate_sequence": candidate.get("sequence"),
              **{key: build.get(key) for key in ("phase", "status", "reason")},
              "review_stale": build.get("review_stale") is True,
              "structural_ready": (candidate.get("readiness") or {}).get("ready") is True}
    if build.get("status") in {"needs_review", "failed"}:
        failure = _failure({"failure": build.get("failure")}, "build")
        for phase in ("final_review", "review"):
            failure = failure or _failure(build.get(phase), phase)
        if failure:
            result["failure"] = failure
    for review in reversed(build.get("review_history") or []):
        failure = _failure(review, "review_history")
        if failure:
            result["previous_review_failure"] = failure
            break
    return result
