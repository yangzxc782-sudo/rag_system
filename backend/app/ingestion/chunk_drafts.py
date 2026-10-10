from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ChunkBlockRef:
    """An ordered reference to a real persisted block; contains no ORM object."""

    block_id: UUID
    block_order: int


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """Neutral chunk values awaiting persistence by a later ingestion stage.

    Legacy drafts may have neither a parse run nor block references.
    PDF ChunkSets supply frozen source references. Source freezing never writes chunks.
    Metadata belongs to this draft and must not be shared with another draft.
    """

    chunk_index: int
    content: str = field(repr=False)
    token_count: int | None
    page_start: int | None
    page_end: int | None
    section_title: str | None
    chunk_type: str | None
    chunk_method: str | None
    content_format: str | None
    source_metadata: dict[str, Any]
    parse_run_id: UUID | None = None
    block_refs: tuple[ChunkBlockRef, ...] = ()
