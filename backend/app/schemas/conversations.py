"""Public local conversation API. No client-supplied history or execution state."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.rag import RagCitationItem, RagGraphData, RagLlmInfo
from app.schemas.casting_answer import CastingAnswerInfo


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionCreateRequest(RequestModel):
    request_id: UUID


class SessionUpdateRequest(RequestModel):
    title: str = Field(min_length=1, max_length=255)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value):
        if not value.strip():
            raise ValueError("Title must not be blank")
        return value.strip()


class TurnCreateRequest(RequestModel):
    request_id: UUID
    question: str = Field(min_length=1, max_length=2000)
    limit: int | None = Field(default=None, ge=1, le=50, strict=True)
    document_id: UUID | None = None
    casting_input_file_id: UUID | None = None

    @field_validator("question")
    @classmethod
    def valid_question(cls, value):
        if not value.strip() or len(value.encode("utf-8")) > 6000:
            raise ValueError("Question is blank or exceeds the input budget")
        return value  # Preserve the exact idempotency input, including whitespace.


TurnStatus = Literal["running", "finalizing", "completed", "failed", "needs_recovery"]
Outcome = Literal["answer", "no_context", "clarification", "casting_design"]


class SessionCreateResponse(BaseModel):
    thread_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class SessionListResponse(BaseModel):
    items: list[SessionCreateResponse]
    next_cursor: str | None = None


class EvidenceSourceStatus(BaseModel):
    snapshot_id: UUID
    kind: Literal["citation", "graph"]
    citation_id: int | None = None
    document_ids: list[UUID]
    chunk_ids: list[UUID]
    status: Literal["available", "source_deleted", "source_unavailable"]


class ConversationAnswer(BaseModel):
    question: str
    answer: str
    context_status: Literal["ok", "no_context", "clarification", "casting_design"]
    citations: list[RagCitationItem]
    graph: RagGraphData | None = None
    llm: RagLlmInfo
    sources: list[EvidenceSourceStatus]
    casting: CastingAnswerInfo | None = None


class RequestStatusResponse(BaseModel):
    thread_id: UUID
    turn_id: UUID
    request_id: UUID
    status: TurnStatus
    outcome: Outcome | None = None
    error_code: str | None = None
    can_retry: bool
    execution_active: bool
    status_url: str
    user_message_id: UUID
    assistant_message_id: UUID | None = None
    result: ConversationAnswer | None = None
    # Saved execution parameters let a fresh browser retry without guessing the
    # effective retrieval limit. This is input metadata, never graph state.
    input: TurnCreateRequest | None = None


class TurnCreateResponse(RequestStatusResponse):
    pass


class SessionDetailResponse(SessionCreateResponse):
    active_request: RequestStatusResponse | None = None


class ConversationMessage(BaseModel):
    message_id: UUID
    sequence_no: int
    # New turns only use user/assistant. Preserve pre-0009 legacy role values.
    role: str = Field(max_length=50)
    content: str
    created_at: datetime
    turn_id: UUID | None = None
    request_id: UUID | None = None
    status: TurnStatus | None = None
    outcome: Outcome | None = None
    result: ConversationAnswer | None = None
    casting_input_file_id: UUID | None = None
    effective_casting_input_file_id: UUID | None = None
    casting_input_filename: str | None = None


class MessageHistoryResponse(BaseModel):
    thread_id: UUID
    items: list[ConversationMessage]
    next_before_seq: int | None = None
