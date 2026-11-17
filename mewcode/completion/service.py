from __future__ import annotations

import hashlib
import hmac
import logging
import math
import operator
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from mewcode.completion.analyzer import (
    JavaCompletionContext,
    JavaLightweightAnalyzer,
)
from mewcode.completion.catalog import (
    COMMON_JAVA_ALIASES,
    JavaMember,
    catalog_members,
    simple_type_name,
)
from mewcode.completion.models import (
    CompletionAnalysis,
    CompletionCandidate,
    CompletionRequest,
    CompletionResponse,
)
from mewcode.completion.store import CompletionPreferenceStore, PreferenceStats


log = logging.getLogger(__name__)

FallbackProvider = Callable[
    [CompletionRequest, JavaCompletionContext],
    Awaitable[list[JavaMember]],
]


class CompletionGraphState(TypedDict, total=False):
    request: CompletionRequest
    completion_id: str
    context: JavaCompletionContext
    raw_candidates: list[JavaMember]
    ranked_candidates: list[CompletionCandidate]
    fallback_used: bool
    graph_path: Annotated[list[str], operator.add]


@dataclass
class _PendingCompletion:
    created_at: float
    language: str
    context_key: str
    candidates: dict[str, str]
    reported_candidates: set[str] = field(default_factory=set)


class CompletionService:
    """A deterministic LangGraph completion workflow with optional LLM fallback."""

    GRAPH_NODES = ("analyze", "learn_alias", "retrieve", "fallback", "rank")
    _PENDING_TTL_SECONDS = 600.0
    _PENDING_LIMIT = 2048
    _PREFERENCE_CACHE_TTL_SECONDS = 15.0
    _PREFERENCE_CACHE_LIMIT = 512
    _SEEN_ALIAS_LIMIT = 4096

    def __init__(
        self,
        work_dir: Path,
        *,
        user_store_path: Path | None = None,
        fallback_provider: FallbackProvider | None = None,
    ) -> None:
        self.work_dir = work_dir.resolve()
        self.analyzer = JavaLightweightAnalyzer()
        self.project_store = CompletionPreferenceStore(
            self.work_dir / ".mewcode" / "completion.db"
        )
        user_path = user_store_path or Path.home() / ".mewcode" / "completion.db"
        self.user_store = (
            self.project_store
            if user_path.resolve() == self.project_store.path.resolve()
            else CompletionPreferenceStore(user_path)
        )
        self.fallback_provider = fallback_provider
        self._candidate_secret = secrets.token_bytes(32)
        self._pending: dict[str, _PendingCompletion] = {}
        self._pending_lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._preference_cache: OrderedDict[
            tuple[str, str, tuple[str, ...]],
            tuple[float, dict[str, PreferenceStats]],
        ] = OrderedDict()
        self._seen_alias_evidence: OrderedDict[
            tuple[str, str, str], None
        ] = OrderedDict()
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(CompletionGraphState)
        builder.add_node("analyze", self._analyze)
        builder.add_node("learn_alias", self._learn_alias)
        builder.add_node("retrieve", self._retrieve)
        builder.add_node("fallback", self._fallback)
        builder.add_node("rank", self._rank)
        builder.add_edge(START, "analyze")
        builder.add_edge("analyze", "learn_alias")
        builder.add_edge("learn_alias", "retrieve")
        builder.add_conditional_edges(
            "retrieve",
            self._route_after_retrieval,
            {"fallback": "fallback", "rank": "rank"},
        )
        builder.add_edge("fallback", "rank")
        builder.add_edge("rank", END)
        return builder.compile()

    def _analyze(self, state: CompletionGraphState) -> CompletionGraphState:
        request = state["request"]
        context = self.analyzer.analyze(
            request.source,
            request.effective_cursor_offset,
        )
        return {"context": context, "graph_path": ["analyze"]}

    def _learn_alias(self, state: CompletionGraphState) -> CompletionGraphState:
        context = state["context"]
        if (
            context.completion_kind == "member"
            and context.resolved_type
            and context.declaration_evidence_hash
            and context.type_confidence >= 0.9
        ):
            evidence_key = (
                context.receiver,
                context.resolved_type,
                context.declaration_evidence_hash,
            )
            with self._cache_lock:
                if evidence_key in self._seen_alias_evidence:
                    self._seen_alias_evidence.move_to_end(evidence_key)
                    return {"graph_path": ["learn_alias"]}

            persisted = True
            for store in self._stores():
                try:
                    store.observe_alias(
                        "java",
                        context.receiver,
                        context.resolved_type,
                        context.declaration_evidence_hash,
                    )
                except sqlite_store_errors():
                    persisted = False
                    log.exception("Failed to persist completion alias observation")
            if persisted:
                with self._cache_lock:
                    self._seen_alias_evidence[evidence_key] = None
                    while len(self._seen_alias_evidence) > self._SEEN_ALIAS_LIMIT:
                        self._seen_alias_evidence.popitem(last=False)
        return {"graph_path": ["learn_alias"]}

    def _retrieve(self, state: CompletionGraphState) -> CompletionGraphState:
        context = state["context"]
        if context.completion_kind != "member":
            return {"raw_candidates": [], "graph_path": ["retrieve"]}

        if not context.resolved_type:
            learned_type = self._resolve_learned_alias(context.receiver)
            if learned_type is not None:
                type_name, confidence = learned_type
                context = context.with_resolved_type(
                    type_name,
                    "learned_alias",
                    confidence,
                )
            elif context.receiver in COMMON_JAVA_ALIASES:
                context = context.with_resolved_type(
                    COMMON_JAVA_ALIASES[context.receiver],
                    "name_heuristic",
                    0.42,
                )

        candidates: list[JavaMember] = []
        if context.resolved_type:
            owner = simple_type_name(context.resolved_type)
            candidates.extend(catalog_members(owner))
            candidates.extend(
                member
                for member in context.project_members
                if simple_type_name(member.declaring_type) == owner
            )

        prefix = context.prefix
        candidates = [
            candidate
            for candidate in candidates
            if candidate.name.startswith(prefix)
            and (not context.static_access or candidate.is_static)
        ]
        return {
            "context": context,
            "raw_candidates": candidates,
            "graph_path": ["retrieve"],
        }

    def _route_after_retrieval(self, state: CompletionGraphState) -> str:
        if self.fallback_provider is None:
            return "rank"
        context = state["context"]
        if not state.get("raw_candidates") or context.type_confidence < 0.6:
            return "fallback"
        return "rank"

    async def _fallback(self, state: CompletionGraphState) -> CompletionGraphState:
        candidates = list(state.get("raw_candidates", []))
        if self.fallback_provider is not None:
            try:
                fallback_candidates = await self.fallback_provider(
                    state["request"],
                    state["context"],
                )
                candidates.extend(fallback_candidates[:50])
            except Exception:
                log.exception("Completion fallback provider failed")
        return {
            "raw_candidates": candidates,
            "fallback_used": True,
            "graph_path": ["fallback"],
        }

    def _rank(self, state: CompletionGraphState) -> CompletionGraphState:
        context = state["context"]
        request = state["request"]
        completion_id = state["completion_id"]

        deduplicated: dict[str, JavaMember] = {}
        for candidate in state.get("raw_candidates", []):
            previous = deduplicated.get(candidate.candidate_key)
            if previous is None or candidate.priority > previous.priority:
                deduplicated[candidate.candidate_key] = candidate
        candidates = list(deduplicated.values())
        candidate_keys = [candidate.candidate_key for candidate in candidates]
        user_stats = self._preference_stats(
            self.user_store,
            context.context_key,
            candidate_keys,
        )
        project_stats = self._preference_stats(
            self.project_store,
            context.context_key,
            candidate_keys,
        )

        ranked: list[CompletionCandidate] = []
        for candidate in candidates:
            score = self._candidate_score(
                candidate,
                context,
                user_stats.get(candidate.candidate_key, PreferenceStats()),
                project_stats.get(candidate.candidate_key, PreferenceStats()),
            )
            candidate_id = self._candidate_id(completion_id, candidate.candidate_key)
            ranked.append(
                CompletionCandidate(
                    candidate_id=candidate_id,
                    label=candidate.label,
                    insert_text=candidate.insert_text,
                    kind=candidate.kind,
                    detail=candidate.detail,
                    score=round(score, 4),
                    source=candidate.source,
                    replacement_start=context.replacement_start,
                    replacement_end=context.replacement_end,
                )
            )

        ranked.sort(key=lambda item: (-item.score, item.label))
        return {
            "ranked_candidates": ranked[: request.max_results],
            "graph_path": ["rank"],
        }

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        started_at = time.perf_counter()
        completion_id = secrets.token_urlsafe(18)
        result = await self.graph.ainvoke(
            {
                "request": request,
                "completion_id": completion_id,
                "fallback_used": False,
                "graph_path": [],
            }
        )
        context: JavaCompletionContext = result["context"]
        candidates: list[CompletionCandidate] = result.get("ranked_candidates", [])
        top_score = candidates[0].score if candidates else 0.0
        self._remember_pending(
            completion_id,
            request.language,
            context.context_key,
            candidates,
            result.get("raw_candidates", []),
        )
        return CompletionResponse(
            completion_id=completion_id,
            candidates=candidates,
            analysis=CompletionAnalysis(
                completion_kind=(
                    "member" if context.completion_kind == "member" else "none"
                ),
                receiver=context.receiver,
                prefix=context.prefix,
                resolved_type=context.resolved_type,
                type_inference=context.type_inference,
                confidence=top_score,
                graph_path=result.get("graph_path", []),
            ),
            fallback_used=result.get("fallback_used", False),
            latency_ms=round((time.perf_counter() - started_at) * 1000, 3),
        )

    def record_feedback(
        self,
        completion_id: str,
        candidate_id: str,
        accepted: bool,
    ) -> bool | None:
        with self._pending_lock:
            self._prune_pending_locked()
            pending = self._pending.get(completion_id)
            if pending is None:
                return None
            candidate_key = pending.candidates.get(candidate_id)
            if candidate_key is None:
                return None
            if candidate_id in pending.reported_candidates:
                return False
            pending.reported_candidates.add(candidate_id)

        persisted = True
        for store in self._stores():
            try:
                store.record_feedback(
                    pending.language,
                    pending.context_key,
                    candidate_key,
                    accepted,
                )
            except sqlite_store_errors():
                persisted = False
                log.exception("Failed to persist completion feedback")
        self._invalidate_preference_cache(pending.context_key)
        return persisted

    @property
    def llm_fallback_configured(self) -> bool:
        return self.fallback_provider is not None

    def _resolve_learned_alias(self, alias: str) -> tuple[str, float] | None:
        scores: dict[str, float] = {}
        try:
            for match in self.project_store.resolve_alias("java", alias):
                scores[match.type_name] = scores.get(match.type_name, 0.0) + (
                    2.0 * match.observations
                )
            for match in self.user_store.resolve_alias("java", alias):
                scores[match.type_name] = scores.get(match.type_name, 0.0) + (
                    1.0 * match.observations
                )
        except sqlite_store_errors():
            log.exception("Failed to retrieve completion alias")
            return None
        if not scores:
            return None
        type_name, score = max(scores.items(), key=lambda item: (item[1], item[0]))
        confidence = min(0.78, 0.56 + math.log1p(score) * 0.055)
        return type_name, confidence

    def _preference_stats(
        self,
        store: CompletionPreferenceStore,
        context_key: str,
        candidate_keys: list[str],
    ) -> dict[str, PreferenceStats]:
        cache_key = (
            str(store.path.resolve()),
            context_key,
            tuple(sorted(candidate_keys)),
        )
        now = time.monotonic()
        with self._cache_lock:
            cached = self._preference_cache.get(cache_key)
            if cached is not None and (
                now - cached[0] < self._PREFERENCE_CACHE_TTL_SECONDS
            ):
                self._preference_cache.move_to_end(cache_key)
                return cached[1]
        try:
            value = store.get_preferences("java", context_key, candidate_keys)
        except sqlite_store_errors():
            log.exception("Failed to retrieve completion preferences")
            return {}
        with self._cache_lock:
            self._preference_cache[cache_key] = (now, value)
            while len(self._preference_cache) > self._PREFERENCE_CACHE_LIMIT:
                self._preference_cache.popitem(last=False)
        return value

    def _invalidate_preference_cache(self, context_key: str) -> None:
        with self._cache_lock:
            stale_keys = [
                cache_key
                for cache_key in self._preference_cache
                if cache_key[1] == context_key
            ]
            for cache_key in stale_keys:
                self._preference_cache.pop(cache_key, None)

    @staticmethod
    def _candidate_score(
        candidate: JavaMember,
        context: JavaCompletionContext,
        user_stats: PreferenceStats,
        project_stats: PreferenceStats,
    ) -> float:
        source_base = {
            "project_symbol": 0.74,
            "jdk_catalog": 0.68,
            "llm": 0.48,
        }[candidate.source]
        score = source_base + 0.18 * candidate.priority
        score *= 0.72 + 0.28 * context.type_confidence
        if candidate.name == context.prefix and context.prefix:
            score += 0.08
        elif context.prefix:
            score += 0.035

        score += CompletionService._stats_boost(user_stats, weight=0.6)
        score += CompletionService._stats_boost(project_stats, weight=0.4)
        return max(0.0, min(1.0, score))

    @staticmethod
    def _stats_boost(stats: PreferenceStats, weight: float) -> float:
        positive = min(0.18, math.log1p(stats.accepted_count) * 0.06)
        negative = min(0.15, math.log1p(stats.rejected_count) * 0.05)
        return weight * (positive - negative)

    def _candidate_id(self, completion_id: str, candidate_key: str) -> str:
        digest = hmac.new(
            self._candidate_secret,
            f"{completion_id}:{candidate_key}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return digest[:24]

    def _remember_pending(
        self,
        completion_id: str,
        language: str,
        context_key: str,
        candidates: list[CompletionCandidate],
        raw_candidates: list[JavaMember],
    ) -> None:
        key_by_id = {
            self._candidate_id(completion_id, candidate.candidate_key): candidate.candidate_key
            for candidate in raw_candidates
        }
        visible = {
            candidate.candidate_id: key_by_id[candidate.candidate_id]
            for candidate in candidates
            if candidate.candidate_id in key_by_id
        }
        with self._pending_lock:
            self._prune_pending_locked()
            self._pending[completion_id] = _PendingCompletion(
                created_at=time.monotonic(),
                language=language,
                context_key=context_key,
                candidates=visible,
            )
            while len(self._pending) > self._PENDING_LIMIT:
                self._pending.pop(next(iter(self._pending)))

    def _prune_pending_locked(self) -> None:
        cutoff = time.monotonic() - self._PENDING_TTL_SECONDS
        expired = [
            completion_id
            for completion_id, pending in self._pending.items()
            if pending.created_at < cutoff
        ]
        for completion_id in expired:
            self._pending.pop(completion_id, None)

    def _stores(self) -> tuple[CompletionPreferenceStore, ...]:
        if self.user_store is self.project_store:
            return (self.project_store,)
        return self.project_store, self.user_store


def sqlite_store_errors() -> tuple[type[Exception], ...]:
    """Keep SQLite optional failure handling local without a broad catch."""

    import sqlite3

    return (sqlite3.Error, OSError)
