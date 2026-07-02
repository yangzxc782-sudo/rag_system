from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class VectorSearchRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=50)
    document_id: UUID | None = None


class VectorSearchItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_id: UUID
    document_id: UUID
    original_filename: str
    chunk_index: int
    content: str
    chunk_type: str | None = None
    source_metadata: dict[str, Any] | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    embedding_status: str
    distance: float
    score: float


class VectorSearchData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    query: str
    limit: int
    document_id: UUID | None = None
    total: int
    items: list[VectorSearchItem]
