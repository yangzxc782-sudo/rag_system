from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    DOCUMENT_ALREADY_PARSED,
    DOCUMENT_NOT_FOUND,
    DOCUMENT_PARSE_FAILED,
    DOCUMENT_PARSER_UNAVAILABLE,
    BusinessError,
)
from app.ingestion import ParsedChunk, Parser, SimpleParser, chunk_parsed_document
from app.ingestion.parsers.mineru import MinerUParser
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.services.object_storage import get_object_bytes_from_minio


@dataclass(frozen=True)
class DocumentParseResult:
    document_id: UUID
    process_status: str
    chunk_count: int
    parser_name: str
    parser_version: str


@dataclass(frozen=True)
class DocumentChunkListResult:
    items: list[DocumentChunk]
    total: int
    limit: int
    offset: int
    stats: dict[str, int | float]


def parse_document(db: Session, document_id: UUID) -> DocumentParseResult:
    document = _get_document_or_raise(db, document_id)
    _ensure_document_has_no_chunks(db, document_id)

    document.process_status = "parsing"
    document.error_message = None
    db.add(document)
    db.commit()
    db.refresh(document)

    try:
        parser = _get_configured_parser()
        content = get_object_bytes_from_minio(
            bucket_name=document.bucket_name,
            object_key=document.object_key,
        )
        parsed_document = parser.parse(
            content=content,
            filename=document.original_filename,
            file_type=document.file_type or "",
            mime_type=document.mime_type,
        )
        parsed_chunks = chunk_parsed_document(
            parsed_document,
            chunk_size_chars=get_settings().chunk_size_chars,
            chunk_overlap_chars=get_settings().chunk_overlap_chars,
        )

        db.add_all(_build_document_chunks(document_id=document.id, parsed_chunks=parsed_chunks))
        document.process_status = "parsed"
        document.error_message = None
        db.add(document)
        db.commit()
        db.refresh(document)
    except BusinessError as exc:
        db.rollback()
        _mark_document_parse_failed(db, document_id, exc.message)
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        message = "文档解析结果写入失败。"
        _mark_document_parse_failed(db, document_id, message)
        raise BusinessError(
            DOCUMENT_PARSE_FAILED,
            message,
            detail={"document_id": str(document_id)},
            status_code=500,
        ) from exc
    except Exception as exc:
        db.rollback()
        message = "文档解析失败。"
        _mark_document_parse_failed(db, document_id, message)
        raise BusinessError(
            DOCUMENT_PARSE_FAILED,
            message,
            detail={
                "document_id": str(document_id),
                "error_type": exc.__class__.__name__,
            },
            status_code=500,
        ) from exc

    return DocumentParseResult(
        document_id=document.id,
        process_status=document.process_status,
        chunk_count=len(parsed_chunks),
        parser_name=parsed_document.parser_name,
        parser_version=parsed_document.parser_version,
    )


def list_document_chunks(
    db: Session,
    document_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> DocumentChunkListResult:
    _get_document_or_raise(db, document_id)

    total = db.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.document_id == document_id),
    ) or 0
    items = list(
        db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
            .limit(limit)
            .offset(offset),
        ).all(),
    )
    all_chunks = list(
        db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc()),
        ).all(),
    )

    return DocumentChunkListResult(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        stats=_build_chunk_stats(all_chunks),
    )


def _get_document_or_raise(db: Session, document_id: UUID) -> Document:
    document = db.get(Document, document_id)

    if document is None:
        raise BusinessError(
            DOCUMENT_NOT_FOUND,
            "文档不存在。",
            detail={"document_id": str(document_id)},
            status_code=404,
        )

    return document


def _ensure_document_has_no_chunks(db: Session, document_id: UUID) -> None:
    chunk_count = db.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.document_id == document_id),
    ) or 0

    if chunk_count > 0:
        raise BusinessError(
            DOCUMENT_ALREADY_PARSED,
            "文档已存在切片，默认不重复解析。",
            detail={"document_id": str(document_id), "chunk_count": chunk_count},
            status_code=409,
        )


def _get_configured_parser() -> Parser:
    settings = get_settings()
    parser_name = settings.document_parser.strip().lower()

    if parser_name == "simple":
        return SimpleParser()

    if parser_name == "mineru":
        return MinerUParser(
            endpoint=settings.mineru_endpoint,
            timeout_seconds=settings.mineru_timeout_seconds,
        )

    raise BusinessError(
        DOCUMENT_PARSER_UNAVAILABLE,
        "未支持的文档解析器配置。",
        detail={"document_parser": settings.document_parser},
        status_code=503,
    )


def _build_document_chunks(*, document_id: UUID, parsed_chunks: list[ParsedChunk]) -> list[DocumentChunk]:
    return [
        DocumentChunk(
            document_id=document_id,
            chunk_index=parsed_chunk.chunk_index,
            content=parsed_chunk.content,
            token_count=None,
            page_start=None,
            page_end=None,
            section_title=None,
            chunk_type=parsed_chunk.chunk_type,
            embedding_model=None,
            embedding_dim=None,
            embedding_status="not_started",
            source_metadata=parsed_chunk.source_metadata,
        )
        for parsed_chunk in parsed_chunks
    ]


def _mark_document_parse_failed(db: Session, document_id: UUID, error_message: str) -> None:
    document = db.get(Document, document_id)
    if document is None:
        return

    document.process_status = "parse_failed"
    document.error_message = error_message
    db.add(document)
    db.commit()


def _build_chunk_stats(chunks: list[DocumentChunk]) -> dict[str, int | float]:
    character_counts = [_get_character_count(chunk) for chunk in chunks]

    if not character_counts:
        return {
            "chunk_count": 0,
            "total_characters": 0,
            "min_characters": 0,
            "max_characters": 0,
            "avg_characters": 0,
        }

    total_characters = sum(character_counts)
    return {
        "chunk_count": len(character_counts),
        "total_characters": total_characters,
        "min_characters": min(character_counts),
        "max_characters": max(character_counts),
        "avg_characters": total_characters / len(character_counts),
    }


def _get_character_count(chunk: DocumentChunk) -> int:
    source_metadata: dict[str, Any] | None = chunk.source_metadata

    if source_metadata is not None:
        character_count = source_metadata.get("character_count")
        if isinstance(character_count, int):
            return character_count
        if isinstance(character_count, float):
            return int(character_count)

    return len(chunk.content)
