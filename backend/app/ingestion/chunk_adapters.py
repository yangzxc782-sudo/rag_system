from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from app.ingestion.block_chunker import ChunkBuildResult
from app.ingestion.chunk_drafts import ChunkBlockRef, ChunkDraft


def adapt_mineru_chunks(result: ChunkBuildResult) -> list[ChunkDraft]:
    """Project existing MinerU output without rechunking, ORM access, or IO.

    Persisted block IDs are mandatory; block keys cannot stand in for them.
    Preserve chunk order, mapping order, content and metadata exactly.
    """
    refs: dict[int, list[ChunkBlockRef]] = {}
    for chunk in result.chunks:
        if chunk.chunk_index in refs:
            raise ValueError("MinerU chunk indexes must be unique")
        refs[chunk.chunk_index] = []
    for link in result.links:
        if link.chunk_index not in refs:
            raise ValueError("Chunk-block mapping references an unknown chunk")
        if link.block_id is None:
            raise ValueError("Chunk-block mapping requires a persisted block id")
        refs[link.chunk_index].append(
            ChunkBlockRef(block_id=UUID(link.block_id), block_order=link.block_order)
        )
    return [
        ChunkDraft(
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            token_count=chunk.estimated_token_count,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            section_title=chunk.section_title,
            chunk_type=chunk.chunk_type,
            chunk_method=chunk.chunk_method,
            content_format=chunk.content_format,
            source_metadata=deepcopy(chunk.source_metadata),
            parse_run_id=UUID(chunk.parse_run_id),
            block_refs=tuple(refs[chunk.chunk_index]),
        )
        for chunk in result.chunks
    ]
