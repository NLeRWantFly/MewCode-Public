"""Serialize synchronous runtime reads with the existing transaction lock."""
from functools import wraps
from inspect import iscoroutinefunction


def runtime_locked(function):
    # Never hold a threading lock across await: two coroutines on the same
    # thread would otherwise both appear to own this re-entrant lock.
    if iscoroutinefunction(function):
        raise TypeError('runtime_locked is only for synchronous state access')

    @wraps(function)
    def read(self, *args, **kwargs):
        runtime = getattr(self, 'runtime', self)
        with runtime.lock:
            return function(self, *args, **kwargs)
    return read
