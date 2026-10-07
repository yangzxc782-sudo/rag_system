from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=50)
    document_id: UUID | None = None


class SearchItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_id: UUID
    document_id: UUID
    original_filename: str
    chunk_index: int
    content: str
    source_metadata: dict[str, Any] | None = None
    retrieval_source: str
    keyword_score: float | None = None
    vector_score: float | None = None
    keyword_rank: int | None = None
    vector_rank: int | None = None
    hybrid_score: float
    matched_keywords: list[str] = Field(default_factory=list)
    embedding_model: str | None = None
    embedding_dim: int | None = None
    source_version: UUID | None = None
    graph_build_id: UUID | None = None
    chunk_set_id: UUID | None = None
    source_start: int | None = None
    source_end: int | None = None
    content_sha256: str | None = None
    embedding_fingerprint: str | None = None


class SearchData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    query: str
    limit: int
    total: int
    items: list[SearchItem]


class SearchIndexCreateData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    index_name: str
    alias: str
    created: bool
    exists: bool
    alias_created: bool
    mapping_status: str
    message: str


class SearchIndexRebuildRequest(BaseModel):
    scope: str = Field(default="all")
    document_id: UUID | None = None


class SearchIndexRebuildData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    scope: str
    document_id: UUID | None = None
    index_name: str
    alias: str
    syncable_chunks: int
    indexed: int
    deleted: int
    failed: int
    errors: list[str] = Field(default_factory=list)
    batch_size: int


class SearchIndexStatusData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    search_engine_available: bool
    index_name: str
    alias: str
    index_exists: bool
    alias_exists: bool
    index_document_count: int
    postgres_syncable_chunks: int
    provider: str
    errors: list[str] = Field(default_factory=list)
