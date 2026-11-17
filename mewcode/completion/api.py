from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, FastAPI, HTTPException

from mewcode.completion.models import (
    CompletionCapabilities,
    CompletionFeedbackRequest,
    CompletionFeedbackResponse,
    CompletionRequest,
    CompletionResponse,
)
from mewcode.completion.service import CompletionService, FallbackProvider


class CompletionServiceRegistry:
    """Lazily creates one service per trusted host-provided project root."""

    def __init__(
        self,
        work_dir_provider: Callable[[], str],
        *,
        user_store_path: Path | None = None,
        fallback_provider: FallbackProvider | None = None,
    ) -> None:
        self._work_dir_provider = work_dir_provider
        self._user_store_path = user_store_path
        self._fallback_provider = fallback_provider
        self._services: dict[Path, CompletionService] = {}
        self._lock = threading.RLock()

    def current(self) -> CompletionService:
        work_dir = Path(self._work_dir_provider()).resolve()
        if not work_dir.is_dir():
            raise RuntimeError(f"Completion work directory does not exist: {work_dir}")
        with self._lock:
            service = self._services.get(work_dir)
            if service is None:
                service = CompletionService(
                    work_dir,
                    user_store_path=self._user_store_path,
                    fallback_provider=self._fallback_provider,
                )
                self._services[work_dir] = service
            return service


def register_completion_api(
    app: FastAPI,
    *,
    work_dir_provider: Callable[[], str],
    user_store_path: Path | None = None,
    fallback_provider: FallbackProvider | None = None,
) -> CompletionServiceRegistry:
    registry = CompletionServiceRegistry(
        work_dir_provider,
        user_store_path=user_store_path,
        fallback_provider=fallback_provider,
    )
    router = APIRouter(prefix="/api/completion", tags=["completion"])

    @router.get("/capabilities", response_model=CompletionCapabilities)
    async def capabilities() -> CompletionCapabilities:
        service = registry.current()
        return CompletionCapabilities(
            languages=["java"],
            graph_nodes=list(service.GRAPH_NODES),
            llm_fallback_configured=service.llm_fallback_configured,
        )

    @router.post("/complete", response_model=CompletionResponse)
    async def complete(request: CompletionRequest) -> CompletionResponse:
        return await registry.current().complete(request)

    @router.post("/feedback", response_model=CompletionFeedbackResponse)
    async def feedback(
        request: CompletionFeedbackRequest,
    ) -> CompletionFeedbackResponse:
        recorded = registry.current().record_feedback(
            request.completion_id,
            request.candidate_id,
            request.accepted,
        )
        if recorded is None:
            raise HTTPException(
                status_code=404,
                detail="Completion or candidate is unknown or expired",
            )
        return CompletionFeedbackResponse(
            recorded=recorded,
            accepted=request.accepted,
        )

    app.include_router(router)
    return registry
