from __future__ import annotations

from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import (
    DOCUMENT_NOT_FOUND,
    DOCUMENT_PARSE_RUN_NOT_FOUND,
    BusinessError,
)
from app.ingestion.mineru.normalizer import NormalizedDocumentBlock
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_parse_run import DocumentParseRun


class DocumentBlockSessionProtocol(Protocol):
    def add_all(self, instances: Sequence[Any]) -> None:
        ...

    def flush(self) -> None:
        ...


@dataclass(frozen=True)
class DocumentBlockSummary:
    id: UUID
    document_id: UUID
    parse_run_id: UUID
    block_index: int
    block_key: str | None
    block_type: str
    page_start: int | None
    page_end: int | None
    bbox: dict[str, Any] | list[Any] | None
    text: str | None
    markdown: str | None
    html: str | None
    latex: str | None
    caption: str | None
    parent_block_key: str | None
    section_path: list[str]
    confidence: float | None
    source_metadata_summary: dict[str, Any]
    content_truncated: bool
    created_at: datetime


@dataclass(frozen=True)
class DocumentBlockListResult:
    items: list[DocumentBlockSummary]
    total: int
    limit: int
    offset: int


def list_blocks(
    db: Session,
    document_id: UUID,
    *,
    parse_run_id: UUID | None = None,
    block_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DocumentBlockListResult:
    _validate_document_and_parse_run(db, document_id, parse_run_id)
    filters = [DocumentBlock.document_id == document_id]
    if parse_run_id is not None:
        filters.append(DocumentBlock.parse_run_id == parse_run_id)
    normalized_type = block_type.strip().lower() if block_type else None
    if normalized_type:
        filters.append(DocumentBlock.block_type == normalized_type)

    total = db.scalar(
        select(func.count())
        .select_from(DocumentBlock)
        .where(*filters)
    ) or 0
    blocks = list(
        db.scalars(
            select(DocumentBlock)
            .where(*filters)
            .order_by(
                DocumentBlock.created_at.desc(),
                DocumentBlock.block_index.asc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
    )
    return DocumentBlockListResult(
        items=[_to_block_summary(block) for block in blocks],
        total=total,
        limit=limit,
        offset=offset,
    )


def build_document_blocks(
    *,
    document_id: UUID,
    parse_run_id: UUID,
    blocks: Iterable[NormalizedDocumentBlock],
) -> list[DocumentBlock]:
    candidates = list(blocks)
    _validate_block_indexes(candidates)
    objects: list[DocumentBlock] = []
    for block in candidates:
        source_metadata = deepcopy(block.source_metadata)
        if block.asset_keys:
            source_metadata["asset_keys"] = list(block.asset_keys)
        objects.append(
            DocumentBlock(
                document_id=document_id,
                parse_run_id=parse_run_id,
                block_index=block.block_index,
                block_key=block.block_key,
                block_type=block.block_type,
                page_start=block.page_start,
                page_end=block.page_end,
                bbox=deepcopy(block.bbox),
                text=block.text,
                markdown=block.markdown,
                html=block.html,
                latex=block.latex,
                caption=block.caption,
                parent_block_key=block.parent_block_key,
                section_path=list(block.section_path),
                confidence=block.confidence,
                source_metadata=source_metadata,
            )
        )
    return objects


def add_document_blocks(
    session: DocumentBlockSessionProtocol,
    *,
    document_id: UUID,
    parse_run_id: UUID,
    blocks: Iterable[NormalizedDocumentBlock],
    flush: bool = True,
) -> list[DocumentBlock]:
    objects = build_document_blocks(
        document_id=document_id,
        parse_run_id=parse_run_id,
        blocks=blocks,
    )
    if objects:
        session.add_all(objects)
        if flush:
            session.flush()
    return objects


def _validate_block_indexes(
    blocks: Sequence[NormalizedDocumentBlock],
) -> None:
    indexes = [block.block_index for block in blocks]
    if len(indexes) != len(set(indexes)):
        raise ValueError("Normalized document block indexes must be unique")
    if indexes != sorted(indexes):
        raise ValueError("Normalized document blocks must use stable index order")


def _validate_document_and_parse_run(
    db: Session,
    document_id: UUID,
    parse_run_id: UUID | None,
) -> None:
    if db.get(Document, document_id) is None:
        raise BusinessError(
            DOCUMENT_NOT_FOUND,
            "文档不存在。",
            detail={"document_id": str(document_id)},
            status_code=404,
        )
    if parse_run_id is None:
        return
    parse_run = db.get(DocumentParseRun, parse_run_id)
    if parse_run is None or parse_run.document_id != document_id:
        raise BusinessError(
            DOCUMENT_PARSE_RUN_NOT_FOUND,
            "解析任务不存在或不属于当前文档。",
            detail={
                "document_id": str(document_id),
                "parse_run_id": str(parse_run_id),
            },
            status_code=404,
        )


def _to_block_summary(block: DocumentBlock) -> DocumentBlockSummary:
    text, text_truncated = _truncate(block.text, 2000)
    markdown, markdown_truncated = _truncate(block.markdown, 2000)
    html, html_truncated = _truncate(block.html, 2000)
    latex, latex_truncated = _truncate(block.latex, 2000)
    caption, caption_truncated = _truncate(block.caption, 500)
    section_path = (
        [str(item)[:200] for item in block.section_path[:20]]
        if isinstance(block.section_path, list)
        else []
    )
    confidence = (
        float(block.confidence) if block.confidence is not None else None
    )
    return DocumentBlockSummary(
        id=block.id,
        document_id=block.document_id,
        parse_run_id=block.parse_run_id,
        block_index=block.block_index,
        block_key=block.block_key,
        block_type=block.block_type,
        page_start=block.page_start,
        page_end=block.page_end,
        bbox=_bbox_summary(block.bbox),
        text=text,
        markdown=markdown,
        html=html,
        latex=latex,
        caption=caption,
        parent_block_key=block.parent_block_key,
        section_path=section_path,
        confidence=confidence,
        source_metadata_summary=_block_metadata_summary(
            block.source_metadata or {}
        ),
        content_truncated=any(
            (
                text_truncated,
                markdown_truncated,
                html_truncated,
                latex_truncated,
                caption_truncated,
            )
        ),
        created_at=block.created_at,
    )


def _truncate(
    value: str | None,
    max_length: int,
) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if len(value) <= max_length:
        return value, False
    return f"{value[:max_length]}...", True


def _block_metadata_summary(
    metadata: dict[str, Any],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in (
        "confidence",
        "heading_level",
        "language",
        "mineru_block_id",
        "original_type",
        "rotation",
    ):
        if key in metadata and isinstance(
            metadata[key],
            (str, int, float, bool),
        ):
            value = metadata[key]
            summary[key] = value[:200] if isinstance(value, str) else value
    asset_keys = metadata.get("asset_keys")
    if isinstance(asset_keys, list):
        summary["asset_keys"] = [
            str(asset_key)[:500] for asset_key in asset_keys[:20]
        ]
    return summary


def _bbox_summary(
    bbox: Any,
) -> dict[str, Any] | list[Any] | None:
    if isinstance(bbox, list):
        return [
            value
            for value in bbox[:20]
            if isinstance(value, (str, int, float, bool))
        ]
    if isinstance(bbox, dict):
        return {
            str(key)[:100]: value
            for key, value in list(bbox.items())[:20]
            if isinstance(value, (str, int, float, bool))
        }
    return None
