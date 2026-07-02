from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    DOCUMENT_EMBEDDINGS_ALREADY_GENERATED,
    DOCUMENT_NOT_FOUND,
    DOCUMENT_NOT_PARSED,
    EMBEDDING_CONFIG_INVALID,
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_GENERATION_FAILED,
    BusinessError,
)
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.retrieval.embeddings import EmbeddingResult, get_embedding_provider


EMBEDDING_STATUS_NOT_STARTED = "not_started"
EMBEDDING_STATUS_EMBEDDING = "embedding"
EMBEDDING_STATUS_EMBEDDED = "embedded"
EMBEDDING_STATUS_FAILED = "embed_failed"
EMBEDDING_TARGET_STATUSES = {EMBEDDING_STATUS_NOT_STARTED, EMBEDDING_STATUS_FAILED}


@dataclass(frozen=True)
class DocumentEmbeddingResult:
    document_id: UUID
    total: int
    embedded: int
    skipped: int
    failed: int
    model: str | None
    dim: int | None
    device: str | None


@dataclass(frozen=True)
class DocumentEmbeddingStatus:
    document_id: UUID
    total: int
    not_started: int
    embedding: int
    embedded: int
    embed_failed: int
    models: list[str]
    dims: list[int]


def generate_document_embeddings(db: Session, document_id: UUID) -> DocumentEmbeddingResult:
    _get_document_or_raise(db, document_id)
    chunks = _list_document_chunks(db, document_id)
    if not chunks:
        raise BusinessError(
            DOCUMENT_NOT_PARSED,
            "Document has no chunks. Parse it before generating embeddings.",
            detail={"document_id": str(document_id)},
            status_code=409,
        )

    target_chunks = [
        chunk
        for chunk in chunks
        if chunk.embedding_status in EMBEDDING_TARGET_STATUSES
    ]
    skipped = sum(1 for chunk in chunks if chunk.embedding_status == EMBEDDING_STATUS_EMBEDDED)

    if not target_chunks:
        if skipped == len(chunks):
            raise BusinessError(
                DOCUMENT_EMBEDDINGS_ALREADY_GENERATED,
                "Document embeddings have already been generated.",
                detail={"document_id": str(document_id)},
                status_code=409,
            )

        raise BusinessError(
            EMBEDDING_GENERATION_FAILED,
            "No chunks are eligible for embedding generation.",
            detail={"document_id": str(document_id), "statuses": _status_counts(chunks)},
            status_code=409,
        )

    settings = get_settings()
    batch_size = _validated_positive_int(
        settings.embedding_batch_size,
        field_name="embedding_batch_size",
    )
    expected_dim = _validated_positive_int(
        settings.embedding_dim,
        field_name="embedding_dim",
    )
    provider = get_embedding_provider(settings)

    try:
        _mark_chunks_embedding(db, target_chunks)
    except Exception as exc:
        db.rollback()
        raise BusinessError(
            EMBEDDING_GENERATION_FAILED,
            "Failed to mark chunks as embedding.",
            detail={"document_id": str(document_id), "error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc

    embedded_count = 0
    last_result: EmbeddingResult | None = None

    try:
        for batch in _batched(target_chunks, batch_size):
            result = provider.encode_documents([chunk.content for chunk in batch])
            _validate_embedding_result(result, batch_size=len(batch), expected_dim=expected_dim)
            now = datetime.now(UTC)

            for chunk, embedding in zip(batch, result.embeddings, strict=True):
                chunk.embedding = embedding
                chunk.embedding_model = result.embedding_model
                chunk.embedding_dim = result.embedding_dim
                chunk.embedding_status = EMBEDDING_STATUS_EMBEDDED
                chunk.embedding_error_message = None
                chunk.embedding_updated_at = now
                db.add(chunk)

            embedded_count += len(batch)
            last_result = result

        db.commit()
    except BusinessError as error:
        db.rollback()
        _mark_chunks_failed(db, target_chunks, error.message)
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        message = "Failed to persist chunk embeddings."
        _mark_chunks_failed(db, target_chunks, message)
        raise BusinessError(
            EMBEDDING_GENERATION_FAILED,
            message,
            detail={"document_id": str(document_id), "error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc
    except Exception as exc:
        db.rollback()
        message = "Embedding generation failed."
        _mark_chunks_failed(db, target_chunks, message)
        raise BusinessError(
            EMBEDDING_GENERATION_FAILED,
            message,
            detail={"document_id": str(document_id), "error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc

    failed_count = len(target_chunks) - embedded_count
    return DocumentEmbeddingResult(
        document_id=document_id,
        total=len(chunks),
        embedded=embedded_count,
        skipped=skipped,
        failed=failed_count,
        model=last_result.embedding_model if last_result else None,
        dim=last_result.embedding_dim if last_result else None,
        device=last_result.device if last_result else None,
    )


def get_document_embedding_status(db: Session, document_id: UUID) -> DocumentEmbeddingStatus:
    _get_document_or_raise(db, document_id)
    chunks = _list_document_chunks(db, document_id)
    counts = _status_counts(chunks)
    models = sorted({chunk.embedding_model for chunk in chunks if chunk.embedding_model})
    dims = sorted({chunk.embedding_dim for chunk in chunks if chunk.embedding_dim is not None})

    return DocumentEmbeddingStatus(
        document_id=document_id,
        total=len(chunks),
        not_started=counts[EMBEDDING_STATUS_NOT_STARTED],
        embedding=counts[EMBEDDING_STATUS_EMBEDDING],
        embedded=counts[EMBEDDING_STATUS_EMBEDDED],
        embed_failed=counts[EMBEDDING_STATUS_FAILED],
        models=models,
        dims=dims,
    )


def _get_document_or_raise(db: Session, document_id: UUID) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise BusinessError(
            DOCUMENT_NOT_FOUND,
            "Document not found.",
            detail={"document_id": str(document_id)},
            status_code=404,
        )
    return document


def _list_document_chunks(db: Session, document_id: UUID) -> list[DocumentChunk]:
    return list(
        db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
        ).all()
    )


def _validated_positive_int(value: int, *, field_name: str) -> int:
    normalized = int(value)
    if normalized <= 0:
        raise BusinessError(
            EMBEDDING_CONFIG_INVALID,
            f"{field_name} must be greater than 0.",
            detail={field_name: normalized},
            status_code=400,
        )
    return normalized


def _mark_chunks_embedding(db: Session, chunks: Iterable[DocumentChunk]) -> None:
    for chunk in chunks:
        chunk.embedding_status = EMBEDDING_STATUS_EMBEDDING
        chunk.embedding_error_message = None
        db.add(chunk)
    db.commit()


def _mark_chunks_failed(db: Session, chunks: Iterable[DocumentChunk], message: str) -> None:
    now = datetime.now(UTC)
    try:
        for chunk in chunks:
            chunk.embedding_status = EMBEDDING_STATUS_FAILED
            chunk.embedding_error_message = message
            chunk.embedding_updated_at = now
            db.add(chunk)
        db.commit()
    except Exception:
        db.rollback()


def _validate_embedding_result(
    result: EmbeddingResult,
    *,
    batch_size: int,
    expected_dim: int,
) -> None:
    if result.embedding_dim != expected_dim:
        raise BusinessError(
            EMBEDDING_DIMENSION_MISMATCH,
            "Embedding result dimension does not match settings.",
            detail={"expected_dim": expected_dim, "actual_dim": result.embedding_dim},
            status_code=500,
        )

    if len(result.embeddings) != batch_size:
        raise BusinessError(
            EMBEDDING_GENERATION_FAILED,
            "Embedding provider returned an unexpected number of vectors.",
            detail={"expected_count": batch_size, "actual_count": len(result.embeddings)},
            status_code=500,
        )

    for index, embedding in enumerate(result.embeddings):
        actual_dim = len(embedding)
        if actual_dim != expected_dim:
            raise BusinessError(
                EMBEDDING_DIMENSION_MISMATCH,
                "Embedding vector dimension does not match settings.",
                detail={"index": index, "expected_dim": expected_dim, "actual_dim": actual_dim},
                status_code=500,
            )


def _batched(items: list[DocumentChunk], batch_size: int) -> Iterable[list[DocumentChunk]]:
    for index in range(0, len(items), batch_size):
        yield items[index : index + batch_size]


def _status_counts(chunks: Iterable[DocumentChunk]) -> dict[str, int]:
    counts = {
        EMBEDDING_STATUS_NOT_STARTED: 0,
        EMBEDDING_STATUS_EMBEDDING: 0,
        EMBEDDING_STATUS_EMBEDDED: 0,
        EMBEDDING_STATUS_FAILED: 0,
    }
    for chunk in chunks:
        status = chunk.embedding_status
        if status in counts:
            counts[status] += 1
    return counts
