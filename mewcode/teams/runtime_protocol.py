"""Typed, bounded payloads for the team coordination runtime."""
from __future__ import annotations

import base64
import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .spec import ContractModel, Identifier, SchemaRef, Text

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_PACKET_BYTES = 24 * 1024 * 1024


class ArtifactContent(ContractModel):
    port_id: Identifier
    schema_ref: SchemaRef
    media_type: Annotated[str, Field(min_length=1, max_length=200)]
    content_b64: Annotated[str, Field(max_length=12 * 1024 * 1024)]

    def content(self) -> bytes:
        try:
            value = base64.b64decode(self.content_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid base64 artifact") from exc
        if not value or len(value) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact must contain 1 byte to 8 MiB")
        return value

    @model_validator(mode="after")
    def validate_content(self) -> ArtifactContent:
        self.content()
        return self

    @classmethod
    def from_bytes(cls, *, port_id: str, schema_ref: str, media_type: str, content: bytes):
        return cls(port_id=port_id, schema_ref=schema_ref, media_type=media_type,
                   content_b64=base64.b64encode(content).decode("ascii"))


class LeaseToken(ContractModel):
    task_id: Text
    attempt_id: Text
    revision: Annotated[int, Field(strict=True, ge=1)]
    fence: Annotated[int, Field(strict=True, ge=1)]
    control_epoch: Annotated[int, Field(strict=True, ge=1)]
    worker_session_id: Text
    lease_token: Text


class Evidence(ContractModel):
    check_id: Identifier
    status: Literal["passed", "failed", "not_run", "observed"]
    detail: Text
    reference: Text | None = None


class HandoffSubmission(ContractModel):
    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    artifacts: Annotated[tuple[ArtifactContent, ...], Field(min_length=1, max_length=64)]
    evidence: Annotated[tuple[Evidence, ...], Field(min_length=1, max_length=128)]

    @model_validator(mode="after")
    def bounded_packet(self) -> HandoffSubmission:
        if len({value.port_id for value in self.artifacts}) != len(self.artifacts):
            raise ValueError("duplicate artifact port")
        if sum(len(value.content()) for value in self.artifacts) > MAX_PACKET_BYTES:
            raise ValueError("handoff exceeds 24 MiB")
        return self


class ReviewScore(ContractModel):
    criterion_id: Identifier
    score: Annotated[int, Field(strict=True, ge=0, le=10000)]
    evidence: Text


def byte_hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
