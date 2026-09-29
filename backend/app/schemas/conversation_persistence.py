"""Internal M1 persistence types, not HTTP or LangGraph schemas."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class PersistenceMetrics(BaseModel):
    """Closed metadata contract: no nested arbitrary text/evidence/cache payload."""
    model_config = ConfigDict(extra="forbid", strict=True)
    latency_ms: int | None = Field(default=None, ge=0)
    candidate_count: int | None = Field(default=None, ge=0)
    budget_tokens: int | None = Field(default=None, ge=0)
    rerank_applied: bool | None = None
    fallback_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{1,100}$")
    provider: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    usage: TokenUsage | None = None


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1)
    outcome: Literal["answer", "no_context", "clarification"]


@dataclass(frozen=True, slots=True)
class EvidenceSourceRef:
    document_id: UUID
    chunk_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SnapshotInput:
    key: str
    kind: Literal["candidate", "citation", "graph", "answer_draft"]
    payload: dict[str, Any]
    sources: tuple[EvidenceSourceRef, ...] = ()
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class EvidenceSnapshotView:
    id: UUID
    session_id: UUID
    turn_id: UUID
    artifact_id: UUID
    kind: str
    status: str
    payload: dict[str, Any] | None
    sources: tuple[EvidenceSourceRef, ...]
    redacted_at: datetime | None
