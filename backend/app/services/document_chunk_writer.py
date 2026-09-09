from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.ingestion.chunk_drafts import ChunkDraft
from app.models.document_block import DocumentBlock
from app.models.document_chunk import DocumentChunk
from app.models.document_chunk_block import DocumentChunkBlock
from app.models.document_parse_run import DocumentParseRun


class DocumentChunkWriter:
    """Write drafts using the caller's transaction and Document write barrier.

    The caller must hold DocumentOperationGuard's lock until commit and roll
    back on any failure. This writer only validates provenance and adds/flushes
    chunks and mappings; it never owns commit, status, or external side effects.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def write(self, *, document_id: UUID, drafts: Sequence[ChunkDraft]) -> list[DocumentChunk]:
        self._validate_references(document_id, drafts)
        objects = [
            DocumentChunk(
                id=uuid4(),
                document_id=document_id,
                parse_run_id=draft.parse_run_id,
                chunk_index=draft.chunk_index,
                content=draft.content,
                token_count=draft.token_count,
                page_start=draft.page_start,
                page_end=draft.page_end,
                section_title=draft.section_title,
                chunk_type=draft.chunk_type,
                chunk_method=draft.chunk_method,
                content_format=draft.content_format,
                source_metadata=deepcopy(draft.source_metadata),
                embedding=None,
                embedding_model=None,
                embedding_dim=None,
                embedding_status="not_started",
            )
            for draft in drafts
        ]
        if objects:
            self._db.add_all(objects)
            self._db.flush()
        mappings = [
            DocumentChunkBlock(
                id=uuid4(), chunk_id=chunk.id,
                block_id=ref.block_id, block_order=ref.block_order,
            )
            for chunk, draft in zip(objects, drafts)
            for ref in draft.block_refs
        ]
        if mappings:
            self._db.add_all(mappings)
            self._db.flush()
        return objects

    def _validate_references(self, document_id: UUID, drafts: Sequence[ChunkDraft]) -> None:
        indexes: set[int] = set()
        runs: dict[UUID, DocumentParseRun] = {}
        blocks: dict[UUID, DocumentBlock] = {}
        for draft in drafts:
            if draft.chunk_index < 0 or draft.chunk_index in indexes:
                raise ValueError("Chunk indexes must be nonnegative and unique")
            indexes.add(draft.chunk_index)
            if draft.parse_run_id is None:
                if draft.block_refs:
                    raise ValueError("Block references require a real parse run")
                continue
            if not isinstance(draft.parse_run_id, UUID):
                raise ValueError("Parse-run reference must be a UUID")
            if draft.parse_run_id not in runs:
                run = self._db.get(DocumentParseRun, draft.parse_run_id)
                if run is None or run.document_id != document_id:
                    raise ValueError("Parse run does not belong to the target document")
                runs[draft.parse_run_id] = run
            orders: set[int] = set()
            for ref in draft.block_refs:
                if not isinstance(ref.block_id, UUID):
                    raise ValueError("Block reference must be a persisted UUID")
                if ref.block_order < 0 or ref.block_order in orders:
                    raise ValueError("Block orders must be nonnegative and unique per chunk")
                orders.add(ref.block_order)
                if ref.block_id not in blocks:
                    block = self._db.get(DocumentBlock, ref.block_id)
                    if block is None:
                        raise ValueError("Referenced block does not exist")
                    blocks[ref.block_id] = block
                block = blocks[ref.block_id]
                if block.document_id != document_id or block.parse_run_id != draft.parse_run_id:
                    raise ValueError("Block does not belong to the target document and parse run")
