"""Process-local invalidation signals; never a second task state store."""
from __future__ import annotations

import logging
import threading

_listeners = set()
_lock = threading.RLock()


def subscribe(listener):
    with _lock:
        _listeners.add(listener)
    def unsubscribe():
        with _lock:
            _listeners.discard(listener)
    return unsubscribe


def changed(kind, **scope):
    """Post-commit hint. A renderer failure must not fail a host transaction."""
    with _lock:
        listeners = tuple(_listeners)
    for listener in listeners:
        try:
            listener(dict(kind=kind, **scope))
        except Exception:
            logging.getLogger(__name__).debug('Workbench notification unavailable', exc_info=True)
