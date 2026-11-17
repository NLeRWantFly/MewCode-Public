"""Disposable FastAPI mounts for host-side API plugins."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from mewcode.cordis import PluginSpec
from mewcode.plugins.services import WEB_API_SLOT


@dataclass(frozen=True)
class FastAPIInstallResult:
    """Optional service and disposer returned by a route installer."""

    service: Any = None
    cleanup: Callable[[], Any] | None = None


def fastapi_plugin(
    name: str,
    app: Any,
    installer: Callable[[], Any],
    *,
    requires: tuple[str, ...] = ("core.runtime",),
    purpose: str = "FastAPI extension",
    service_definition: Any = None,
) -> PluginSpec:
    """Turn an existing route installer into a lifecycle-owned plugin.

    FastAPI does not expose route disposal as a first-class operation, so the
    adapter records the route list before installation and removes only routes
    created by this plugin during teardown.
    """

    def apply(ctx: Any) -> Callable[[], Any]:
        before = list(app.router.routes)
        old_lifespan = getattr(app.router, "lifespan_context", None)
        installed_cleanup: Callable[[], Any] | None = None

        def restore_routes() -> None:
            app.router.routes[:] = [route for route in app.router.routes if route in before]
            if old_lifespan is not None:
                app.router.lifespan_context = old_lifespan

        def cleanup() -> None:
            result = installed_cleanup() if installed_cleanup is not None else None
            if inspect.isawaitable(result):
                async def finish() -> None:
                    try:
                        await result
                    finally:
                        restore_routes()

                return finish()
            restore_routes()
            return result

        try:
            installed = installer()
            if isinstance(installed, FastAPIInstallResult):
                service = installed.service
                installed_cleanup = installed.cleanup
            else:
                service = installed
            ctx.provide(f"web.api.{name}", service, replace=True)
            if service_definition is not None:
                ctx.provide(service_definition, service, replace=True)
            ctx.slots.register(WEB_API_SLOT, service, kind="list")
        except Exception:
            cleanup_result = cleanup()
            if inspect.isawaitable(cleanup_result):
                ctx._schedule_awaitable(cleanup_result, label=f"FastAPI plugin {name} rollback")
            raise

        return cleanup

    return PluginSpec(
        name,
        apply,
        requires=requires,
        version="1",
        purpose=purpose,
        source="builtin",
    )


__all__ = ["FastAPIInstallResult", "fastapi_plugin"]
