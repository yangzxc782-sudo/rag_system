from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.document_chunk import DocumentChunk


class DocumentParseData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: UUID
    process_status: str
    chunk_count: Literal[0]
    parser_name: str
    parser_version: str
    parse_run_id: UUID
    source_version: UUID
    canonical_sha256: str
    character_count: int
    block_count: int


class DocumentEmbeddingData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: UUID
    total: int
    embedded: int
    skipped: int
    failed: int
    model: str | None = None
    dim: int | None = None
    device: str | None = None


class DocumentEmbeddingStatusData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: UUID
    total: int
    not_started: int
    embedding: int
    embedded: int
    embed_failed: int
    models: list[str]
    dims: list[int]


class DocumentChunkRead(BaseModel):
    id: UUID
    document_id: UUID
    chunk_index: int
    content: str
    character_count: int
    token_count: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    section_title: str | None = None
    chunk_type: str | None = None
    chunk_method: str | None = None
    content_format: str | None = None
    source_metadata: dict[str, Any] | None = None
    chunk_set_id: UUID | None = None
    source_version: UUID | None = None
    source_start: int | None = None
    source_end: int | None = None
    content_sha256: str | None = None
    embedding_status: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_chunk(cls, chunk: DocumentChunk) -> "DocumentChunkRead":
        return cls(
            id=chunk.id,
            document_id=chunk.document_id,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            character_count=get_chunk_character_count(chunk),
            token_count=chunk.token_count,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            section_title=chunk.section_title,
            chunk_type=chunk.chunk_type,
            chunk_method=chunk.chunk_method,
            content_format=chunk.content_format,
            source_metadata=chunk.source_metadata,
            **{key: chunk.__dict__.get(key) for key in ("chunk_set_id", "source_version", "source_start", "source_end", "content_sha256")},
            embedding_status=chunk.embedding_status,
            created_at=chunk.created_at,
            updated_at=chunk.updated_at,
        )


class DocumentChunkStats(BaseModel):
    chunk_count: int
    total_characters: int
    min_characters: int
    max_characters: int
    avg_characters: float


class DocumentChunkListData(BaseModel):
    items: list[DocumentChunkRead]
    total: int
    limit: int
    offset: int
    stats: DocumentChunkStats


def get_chunk_character_count(chunk: DocumentChunk) -> int:
    source_metadata = chunk.source_metadata

    if source_metadata is not None:
        character_count = source_metadata.get("character_count")
        if isinstance(character_count, int):
            return character_count
        if isinstance(character_count, float):
            return int(character_count)

    return len(chunk.content)
