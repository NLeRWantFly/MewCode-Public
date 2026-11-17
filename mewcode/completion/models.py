from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


MAX_SOURCE_CHARS = 256_000

CompletionKind = Literal["member", "none"]
CandidateKind = Literal["method", "field", "snippet"]
CandidateSource = Literal["jdk_catalog", "project_symbol", "llm"]


class CompletionRequest(BaseModel):
    """One editor completion request.

    ``source`` is deliberately bounded. The endpoint does not accept a project
    root or an arbitrary file path; the host application owns that trust
    boundary.
    """

    language: Literal["java"] = "java"
    source: str = Field(default="", max_length=MAX_SOURCE_CHARS)
    cursor_offset: int | None = Field(default=None, ge=0)
    max_results: int = Field(default=12, ge=1, le=50)

    @model_validator(mode="after")
    def validate_cursor(self) -> CompletionRequest:
        if self.cursor_offset is not None and self.cursor_offset > len(self.source):
            raise ValueError("cursor_offset cannot exceed source length")
        return self

    @property
    def effective_cursor_offset(self) -> int:
        return len(self.source) if self.cursor_offset is None else self.cursor_offset


class CompletionCandidate(BaseModel):
    candidate_id: str
    label: str
    insert_text: str
    kind: CandidateKind
    detail: str = ""
    score: float = Field(ge=0.0, le=1.0)
    source: CandidateSource
    replacement_start: int = Field(ge=0)
    replacement_end: int = Field(ge=0)


class CompletionAnalysis(BaseModel):
    completion_kind: CompletionKind
    receiver: str = ""
    prefix: str = ""
    resolved_type: str = ""
    type_inference: str = "unresolved"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    graph_path: list[str] = Field(default_factory=list)


class CompletionResponse(BaseModel):
    completion_id: str
    candidates: list[CompletionCandidate]
    analysis: CompletionAnalysis
    fallback_used: bool = False
    latency_ms: float = Field(ge=0.0)


class CompletionFeedbackRequest(BaseModel):
    completion_id: str = Field(
        min_length=12,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    candidate_id: str = Field(
        min_length=12,
        max_length=80,
        pattern=r"^[a-f0-9]+$",
    )
    accepted: bool


class CompletionFeedbackResponse(BaseModel):
    recorded: bool
    accepted: bool


class CompletionCapabilities(BaseModel):
    languages: list[str]
    graph_nodes: list[str]
    llm_fallback_configured: bool
    source_retention: Literal["request_only"] = "request_only"
    learning_storage: Literal["aggregate_only"] = "aggregate_only"
