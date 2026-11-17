"""Host-only, task-local native lifecycle observations (never authority)."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import logging
import uuid

observer: ContextVar = ContextVar("native_execution_progress_observer", default=None)
_operation: ContextVar = ContextVar("native_execution_operation", default=None)
_TERMINAL = {"finished", "not_started", "cleanup_unknown", "interrupted"}


def emit(phase: str, **facts) -> None:
    state = _operation.get()
    if state is None:
        return
    state.update(facts, phase=phase, updated_at=datetime.now(timezone.utc).isoformat())
    callback = observer.get()
    if callback is not None:
        try:
            callback(dict(state))
        except Exception:
            logging.getLogger(__name__).exception("Native progress observer failed")


def finish(receipt) -> None:
    state = _operation.get()
    if state is None:
        return
    cleanup = (receipt.cleanup_confirmed if receipt.cleanup_confirmed is not None
               else state.get("cleanup_confirmed"))
    phase = ("finished" if cleanup is True else
             "cleanup_unknown" if state.get("helper_pid") else "not_started")
    emit(phase, cleanup_confirmed=cleanup,
         exit_code=receipt.exit_code, outcome=receipt.status)


@contextmanager
def operation():
    token = _operation.set({"execution_id": uuid.uuid4().hex,
                            "process_started": False, "cleanup_confirmed": None})
    try:
        emit("queued")
        yield
    finally:
        state = _operation.get()
        if state.get("phase") not in _TERMINAL:
            emit("cleanup_unknown" if state.get("helper_pid") else "not_started")
        _operation.reset(token)


def restored(value: object) -> dict:
    """Historical startup facts are not evidence of a currently live process."""
    if not isinstance(value, dict) or not value:
        return {}
    result = dict(value)
    if result.get("phase") not in _TERMINAL:
        result.update(phase="interrupted", cleanup_confirmed=None)
    return result
