from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    DOCUMENT_NOT_FOUND,
    EMBEDDING_CONFIG_INVALID,
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_GENERATION_FAILED,
    VECTOR_SEARCH_FAILED,
    BusinessError,
)
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.retrieval.embeddings import EmbeddingResult, get_embedding_provider


MAX_VECTOR_SEARCH_LIMIT = 50
EMBEDDING_STATUS_EMBEDDED = "embedded"
_MISSING = object()


@dataclass(frozen=True)
class VectorSearchItem:
    chunk_id: UUID
    document_id: UUID
    original_filename: str
    chunk_index: int
    content: str
    chunk_type: str | None
    source_metadata: dict[str, Any] | None
    embedding_model: str | None
    embedding_dim: int | None
    embedding_status: str
    distance: float
    score: float


@dataclass(frozen=True)
class VectorSearchResult:
    query: str
    limit: int
    document_id: UUID | None
    total: int
    items: list[VectorSearchItem]


def vector_search_chunks(
    db: Session,
    *,
    query: str,
    limit: int = 10,
    document_id: UUID | None = None,
) -> VectorSearchResult:
    normalized_query = query.strip()
    if not normalized_query:
        raise BusinessError(
            EMBEDDING_CONFIG_INVALID,
            "Search query must not be empty.",
            status_code=400,
        )

    normalized_limit = _validate_limit(limit)
    if document_id is not None and db.get(Document, document_id) is None:
        raise BusinessError(
            DOCUMENT_NOT_FOUND,
            "Document not found.",
            detail={"document_id": str(document_id)},
            status_code=404,
        )

    settings = get_settings()
    expected_dim = _validate_embedding_dim_config(settings.embedding_dim)
    provider = get_embedding_provider(settings)

    try:
        embedding_result = provider.encode_query(normalized_query)
        query_embedding = _extract_query_embedding(embedding_result, expected_dim=expected_dim)
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            EMBEDDING_GENERATION_FAILED,
            "Query embedding generation failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc

    try:
        rows = _query_vector_rows(
            db,
            query_embedding=query_embedding,
            embedding_model=embedding_result.embedding_model,
            embedding_dim=embedding_result.embedding_dim,
            limit=normalized_limit,
            document_id=document_id,
        )
    except SQLAlchemyError as exc:
        raise BusinessError(
            VECTOR_SEARCH_FAILED,
            "Vector search query failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc
    except Exception as exc:
        raise BusinessError(
            VECTOR_SEARCH_FAILED,
            "Vector search failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc

    items = [
        _row_to_item(row)
        for row in rows
        if _is_searchable_row(
            row,
            embedding_model=embedding_result.embedding_model,
            embedding_dim=embedding_result.embedding_dim,
        )
    ]
    items.sort(key=lambda item: item.distance)
    items = items[:normalized_limit]

    return VectorSearchResult(
        query=normalized_query,
        limit=normalized_limit,
        document_id=document_id,
        total=len(items),
        items=items,
    )


def _query_vector_rows(
    db: Session,
    *,
    query_embedding: list[float],
    embedding_model: str,
    embedding_dim: int,
    limit: int,
    document_id: UUID | None,
) -> list[Mapping[str, Any]]:
    distance_expr = DocumentChunk.embedding.cosine_distance(query_embedding).label("distance")
    statement = (
        select(
            DocumentChunk.id.label("chunk_id"),
            DocumentChunk.document_id.label("document_id"),
            Document.original_filename.label("original_filename"),
            DocumentChunk.chunk_index.label("chunk_index"),
            DocumentChunk.content.label("content"),
            DocumentChunk.chunk_type.label("chunk_type"),
            DocumentChunk.source_metadata.label("source_metadata"),
            DocumentChunk.embedding_model.label("embedding_model"),
            DocumentChunk.embedding_dim.label("embedding_dim"),
            DocumentChunk.embedding_status.label("embedding_status"),
            distance_expr,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.embedding_status == EMBEDDING_STATUS_EMBEDDED,
            DocumentChunk.embedding.is_not(None),
            DocumentChunk.embedding_model == embedding_model,
            DocumentChunk.embedding_dim == embedding_dim,
        )
    )

    if document_id is not None:
        statement = statement.where(DocumentChunk.document_id == document_id)

    statement = statement.order_by(distance_expr.asc()).limit(limit)

    return list(db.execute(statement).mappings().all())


def _validate_limit(limit: int) -> int:
    normalized = int(limit)
    if normalized < 1 or normalized > MAX_VECTOR_SEARCH_LIMIT:
        raise BusinessError(
            EMBEDDING_CONFIG_INVALID,
            "Search limit must be between 1 and 50.",
            detail={"limit": normalized, "max_limit": MAX_VECTOR_SEARCH_LIMIT},
            status_code=400,
        )
    return normalized


def _validate_embedding_dim_config(value: int) -> int:
    expected_dim = int(value)
    if expected_dim <= 0:
        raise BusinessError(
            EMBEDDING_CONFIG_INVALID,
            "embedding_dim must be greater than 0.",
            detail={"embedding_dim": expected_dim},
            status_code=400,
        )
    return expected_dim


def _extract_query_embedding(result: EmbeddingResult, *, expected_dim: int) -> list[float]:
    if result.embedding_dim != expected_dim:
        raise BusinessError(
            EMBEDDING_DIMENSION_MISMATCH,
            "Query embedding dimension does not match settings.",
            detail={"expected_dim": expected_dim, "actual_dim": result.embedding_dim},
            status_code=500,
        )

    if len(result.embeddings) != 1:
        raise BusinessError(
            EMBEDDING_GENERATION_FAILED,
            "Embedding provider returned an unexpected number of query vectors.",
            detail={"expected_count": 1, "actual_count": len(result.embeddings)},
            status_code=500,
        )

    embedding = result.embeddings[0]
    actual_dim = len(embedding)
    if actual_dim != expected_dim:
        raise BusinessError(
            EMBEDDING_DIMENSION_MISMATCH,
            "Query embedding vector dimension does not match settings.",
            detail={"expected_dim": expected_dim, "actual_dim": actual_dim},
            status_code=500,
        )
    return embedding


def _is_searchable_row(row: Mapping[str, Any], *, embedding_model: str, embedding_dim: int) -> bool:
    if row["embedding_status"] != EMBEDDING_STATUS_EMBEDDED:
        return False

    if row["embedding_model"] != embedding_model:
        return False

    if row["embedding_dim"] != embedding_dim:
        return False

    embedding = row.get("embedding", _MISSING)
    return embedding is _MISSING or embedding is not None


def _row_to_item(row: Mapping[str, Any]) -> VectorSearchItem:
    distance = float(row["distance"])
    return VectorSearchItem(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        original_filename=row["original_filename"],
        chunk_index=row["chunk_index"],
        content=row["content"],
        chunk_type=row["chunk_type"],
        source_metadata=row["source_metadata"],
        embedding_model=row["embedding_model"],
        embedding_dim=row["embedding_dim"],
        embedding_status=row["embedding_status"],
        distance=distance,
        score=1.0 - distance,
    )
