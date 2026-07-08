from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


KNOWLEDGE_ITEM_TYPES = {
    "process_rule",
    "parameter_recommendation",
    "defect_cause",
    "defect_solution",
    "material_property",
    "standard_requirement",
    "term_definition",
    "case_experience",
}

KNOWLEDGE_ITEM_STATUSES = {
    "draft",
    "pending_review",
    "approved",
    "rejected",
    "deprecated",
}

CREATABLE_KNOWLEDGE_ITEM_STATUSES = {"draft", "pending_review"}
EDITABLE_KNOWLEDGE_ITEM_STATUSES = {"draft", "rejected"}

JsonLike = dict[str, Any] | list[Any]


class KnowledgeItemCreate(BaseModel):
    item_type: str
    title: str
    content: str
    structured_data: dict[str, Any] | None = None
    entities: JsonLike | None = None
    parameters: JsonLike | None = None
    conditions: JsonLike | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: str | None = None
    source_document_id: UUID | None = None
    source_chunk_ids: list[UUID] | None = None
    source_filename: str | None = None
    created_by: str | None = None


class KnowledgeItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    content: str | None = None
    structured_data: dict[str, Any] | None = None
    entities: JsonLike | None = None
    parameters: JsonLike | None = None
    conditions: JsonLike | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_chunk_ids: list[UUID] | None = None
    source_filename: str | None = None
    updated_by: str | None = None
    change_reason: str | None = None


class KnowledgeItemData(BaseModel):
    id: UUID
    item_type: str
    title: str
    content: str
    content_hash: str
    structured_data: dict[str, Any] | None = None
    entities: JsonLike | None = None
    parameters: JsonLike | None = None
    conditions: JsonLike | None = None
    confidence: float | None = None
    status: str
    source_document_id: UUID | None = None
    source_filename: str | None = None
    source_chunk_ids: list[UUID]
    created_by: str | None = None
    reviewed_by: str | None = None
    review_comment: str | None = None
    version: int
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    revises_item_id: UUID | None = None

    @classmethod
    def from_item(cls, item: Any) -> "KnowledgeItemData":
        chunks = list(getattr(item, "chunks", []) or [])
        return cls(
            id=getattr(item, "id"),
            item_type=str(getattr(item, "item_type")),
            title=str(getattr(item, "title")),
            content=str(getattr(item, "content")),
            content_hash=str(getattr(item, "content_hash")),
            structured_data=getattr(item, "structured_data", None),
            entities=getattr(item, "entities", None),
            parameters=getattr(item, "parameters", None),
            conditions=getattr(item, "conditions", None),
            confidence=confidence_to_float(getattr(item, "confidence", None)),
            status=str(getattr(item, "status")),
            source_document_id=getattr(item, "source_document_id", None),
            source_filename=getattr(item, "source_filename", None),
            source_chunk_ids=[getattr(chunk, "chunk_id") for chunk in chunks],
            created_by=getattr(item, "created_by", None),
            reviewed_by=getattr(item, "reviewed_by", None),
            review_comment=getattr(item, "review_comment", None),
            version=int(getattr(item, "version")),
            reviewed_at=getattr(item, "reviewed_at", None),
            created_at=getattr(item, "created_at"),
            updated_at=getattr(item, "updated_at"),
            revises_item_id=getattr(item, "revises_item_id", None),
        )


class KnowledgeItemListData(BaseModel):
    items: list[KnowledgeItemData]
    total: int
    limit: int
    offset: int


class KnowledgeItemChunkData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    knowledge_item_id: UUID
    chunk_id: UUID
    document_id: UUID
    chunk_index: int
    source_text: str
    created_at: datetime


class KnowledgeItemChunksData(BaseModel):
    items: list[KnowledgeItemChunkData]
    total: int


class KnowledgeItemVersionData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    knowledge_item_id: UUID
    version: int
    snapshot: dict[str, Any]
    change_reason: str | None = None
    created_by: str | None = None
    created_at: datetime


class KnowledgeItemVersionsData(BaseModel):
    items: list[KnowledgeItemVersionData]
    total: int


class KnowledgeItemReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str | None = None
    review_comment: str | None = None


class KnowledgeItemReviewData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    knowledge_item_id: UUID
    review_action: str
    from_status: str
    to_status: str
    review_comment: str | None = None
    reviewer: str | None = None
    created_at: datetime


class KnowledgeItemReviewsData(BaseModel):
    items: list[KnowledgeItemReviewData]
    total: int


class KnowledgeItemReviseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_by: str | None = None
    change_reason: str | None = None


class KnowledgeExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    document_id: UUID | None = None
    chunk_ids: list[UUID] | None = None
    item_types: list[str] | None = None
    auto_submit: bool = False
    max_chunks: int | None = None
    created_by: str | None = None


class ExtractedKnowledgeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_type: str
    title: str
    content: str
    structured_data: dict[str, Any] | None = None
    entities: JsonLike | None = None
    parameters: JsonLike | None = None
    conditions: JsonLike | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_chunk_ids: list[UUID]
    source_text: str | None = None

    @field_validator("title", "content")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("item_type")
    @classmethod
    def validate_item_type(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if normalized not in KNOWLEDGE_ITEM_TYPES:
            raise ValueError("invalid item_type")
        return normalized

    @field_validator("source_chunk_ids")
    @classmethod
    def validate_source_chunk_ids(cls, value: list[UUID]) -> list[UUID]:
        if not value:
            raise ValueError("source_chunk_ids must not be empty")
        return value


class KnowledgeExtractionSkippedDuplicate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_type: str
    title: str
    content_hash: str
    source_document_id: UUID | None = None
    reason: str


class KnowledgeExtractionLlmInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str | None = None
    model: str | None = None


class KnowledgeExtractionData(BaseModel):
    items: list[KnowledgeItemData]
    created: int
    skipped_duplicates: list[KnowledgeExtractionSkippedDuplicate]
    status: str
    auto_submit: bool
    llm: KnowledgeExtractionLlmInfo


def confidence_to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)
