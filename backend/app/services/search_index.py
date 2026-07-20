from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    DOCUMENT_NOT_FOUND,
    SEARCH_ENGINE_CONFIG_INVALID,
    SEARCH_ENGINE_UNAVAILABLE,
    SEARCH_INDEX_CREATE_FAILED,
    SEARCH_INDEX_MAPPING_MISMATCH,
    SEARCH_INDEX_NOT_FOUND,
    SEARCH_INDEX_REBUILD_FAILED,
    BusinessError,
)
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.search_engine.client import SearchEngineClientProtocol, get_search_engine_client
from app.search_engine.index_schema import (
    build_casting_chunks_index_body,
    build_casting_chunks_index_mapping,
    get_index_alias,
    get_index_name,
)


EMBEDDING_STATUS_EMBEDDED = "embedded"
DEFAULT_EMBEDDING_MODEL = "Qwen3-Embedding-0.6B"
DEFAULT_EMBEDDING_DIM = 1024
REBUILD_SCOPE_ALL = "all"
REBUILD_SCOPE_DOCUMENT = "document"

EXACT_TERM_PATTERNS: tuple[tuple[str, re.Pattern[str] | None], ...] = tuple(
    (term, re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.IGNORECASE))
    for term in ("GB/T", "GJB", "HB", "ASTM", "ISO", "HT250", "QT450", "ZG25")
) + tuple(
    (term, None)
    for term in (
        "冒口",
        "冷铁",
        "热节",
        "补缩",
        "浇注系统",
        "浇口",
        "横浇道",
        "直浇道",
        "内浇道",
        "砂芯",
        "芯盒",
        "分型面",
        "缩孔",
        "缩松",
        "夹渣",
        "夹砂",
        "气孔",
        "卷气",
        "冷隔",
        "裂纹",
        "粘砂",
    )
)


@dataclass(frozen=True)
class SyncableChunkRow:
    document: Document
    chunk: DocumentChunk


@dataclass(frozen=True)
class SearchIndexCreateResult:
    index_name: str
    alias: str
    created: bool
    exists: bool
    alias_created: bool
    mapping_status: str
    message: str


@dataclass(frozen=True)
class SearchIndexRebuildResult:
    scope: str
    document_id: UUID | None
    index_name: str
    alias: str
    syncable_chunks: int
    indexed: int
    deleted: int
    failed: int
    errors: list[str] = field(default_factory=list)
    batch_size: int = 100


@dataclass(frozen=True)
class SearchIndexStatusResult:
    search_engine_available: bool
    index_name: str
    alias: str
    index_exists: bool
    alias_exists: bool
    index_document_count: int
    postgres_syncable_chunks: int
    provider: str
    errors: list[str] = field(default_factory=list)


def create_or_update_search_index(
    db: Session,
    *,
    client: SearchEngineClientProtocol | None = None,
    settings: Any | None = None,
) -> SearchIndexCreateResult:
    del db
    settings = settings or get_settings()
    client = client or get_search_engine_client(settings)
    index_name = get_index_name(settings)
    alias = get_index_alias(settings)
    target_body = build_casting_chunks_index_body(settings)
    target_mapping = build_casting_chunks_index_mapping(settings)

    try:
        index_exists = bool(client.indices.exists(index=index_name))
    except Exception as exc:
        raise BusinessError(
            SEARCH_ENGINE_UNAVAILABLE,
            "Search engine is unavailable while checking index.",
            detail=_error_detail(exc),
            status_code=503,
        ) from exc

    if not index_exists:
        try:
            client.indices.create(index=index_name, body=target_body)
        except Exception as exc:
            raise BusinessError(
                SEARCH_INDEX_CREATE_FAILED,
                "Failed to create search index.",
                detail=_error_detail(exc),
                status_code=500,
            ) from exc
        return SearchIndexCreateResult(
            index_name=index_name,
            alias=alias,
            created=True,
            exists=False,
            alias_created=True,
            mapping_status="created",
            message="Search index created.",
        )

    try:
        current_mapping = client.indices.get_mapping(index=index_name)
    except Exception as exc:
        raise BusinessError(
            SEARCH_ENGINE_UNAVAILABLE,
            "Search engine is unavailable while reading index mapping.",
            detail=_error_detail(exc),
            status_code=503,
        ) from exc

    if not _mapping_matches_target(current_mapping, target_mapping, index_name=index_name):
        raise BusinessError(
            SEARCH_INDEX_MAPPING_MISMATCH,
            "Existing search index mapping does not match target mapping.",
            detail={"index_name": index_name, "alias": alias},
            status_code=409,
        )

    alias_created = _ensure_alias(client, index_name=index_name, alias=alias)
    return SearchIndexCreateResult(
        index_name=index_name,
        alias=alias,
        created=False,
        exists=True,
        alias_created=alias_created,
        mapping_status="matched",
        message="Search index already exists and mapping is compatible.",
    )


def rebuild_search_index(
    db: Session,
    *,
    scope: str = REBUILD_SCOPE_ALL,
    document_id: UUID | None = None,
    client: SearchEngineClientProtocol | None = None,
    settings: Any | None = None,
) -> SearchIndexRebuildResult:
    settings = settings or get_settings()
    normalized_scope = _validate_rebuild_scope(scope, document_id)
    client = client or get_search_engine_client(settings)
    index_name = get_index_name(settings)
    alias = get_index_alias(settings)
    batch_size = int(settings.search_index_batch_size)

    if normalized_scope == REBUILD_SCOPE_DOCUMENT:
        if db.get(Document, document_id) is None:
            raise BusinessError(
                DOCUMENT_NOT_FOUND,
                "Document not found.",
                detail={"document_id": str(document_id)},
                status_code=404,
            )

    try:
        if not bool(client.indices.exists(index=index_name)):
            raise BusinessError(
                SEARCH_INDEX_NOT_FOUND,
                "Search index does not exist. Create the index before rebuilding.",
                detail={"index_name": index_name},
                status_code=404,
            )
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            SEARCH_ENGINE_UNAVAILABLE,
            "Search engine is unavailable while checking index.",
            detail=_error_detail(exc),
            status_code=503,
        ) from exc

    deleted = 0
    if normalized_scope == REBUILD_SCOPE_DOCUMENT:
        deleted = _delete_document_from_index(client, index_name=index_name, document_id=document_id)

    rows = _load_syncable_chunks(db, settings=settings, document_id=document_id)
    syncable_chunks = len(rows)
    indexed, failed, errors = _bulk_index_rows(
        client,
        index_name=index_name,
        rows=rows,
        settings=settings,
        batch_size=batch_size,
    )

    return SearchIndexRebuildResult(
        scope=normalized_scope,
        document_id=document_id,
        index_name=index_name,
        alias=alias,
        syncable_chunks=syncable_chunks,
        indexed=indexed,
        deleted=deleted,
        failed=failed,
        errors=errors,
        batch_size=batch_size,
    )


def get_search_index_status(
    db: Session,
    *,
    client: SearchEngineClientProtocol | None = None,
    settings: Any | None = None,
) -> SearchIndexStatusResult:
    settings = settings or get_settings()
    index_name = get_index_name(settings)
    alias = get_index_alias(settings)
    provider = str(settings.search_engine_provider)
    postgres_syncable_chunks = count_postgres_syncable_chunks(db, settings=settings)

    try:
        client = client or get_search_engine_client(settings)
        index_exists = bool(client.indices.exists(index=index_name))
        alias_exists = bool(client.indices.exists_alias(name=alias)) if index_exists else False
        count_response = client.count(index=index_name) if index_exists else {"count": 0}
        index_document_count = int(count_response.get("count", 0))
    except Exception as exc:
        return SearchIndexStatusResult(
            search_engine_available=False,
            index_name=index_name,
            alias=alias,
            index_exists=False,
            alias_exists=False,
            index_document_count=0,
            postgres_syncable_chunks=postgres_syncable_chunks,
            provider=provider,
            errors=[_format_error(exc)],
        )

    return SearchIndexStatusResult(
        search_engine_available=True,
        index_name=index_name,
        alias=alias,
        index_exists=index_exists,
        alias_exists=alias_exists,
        index_document_count=index_document_count,
        postgres_syncable_chunks=postgres_syncable_chunks,
        provider=provider,
        errors=[],
    )


def build_chunk_index_payload(document: Document, chunk: DocumentChunk, settings: Any) -> dict[str, Any]:
    del settings
    return {
        "chunk_id": str(chunk.id),
        "document_id": str(document.id),
        "original_filename": document.original_filename,
        "chunk_index": chunk.chunk_index,
        "content": chunk.content,
        "content_max": chunk.content,
        "content_smart": chunk.content,
        "chunk_type": chunk.chunk_type,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "section_title": chunk.section_title,
        "source_metadata": chunk.source_metadata or {},
        "exact_terms": extract_exact_terms(chunk.content),
        "embedding": _embedding_to_float_list(chunk.embedding),
        "embedding_model": chunk.embedding_model,
        "embedding_dim": chunk.embedding_dim,
        "embedding_status": chunk.embedding_status,
        "document_process_status": document.process_status,
        "created_at": _datetime_to_iso(chunk.created_at),
        "updated_at": _datetime_to_iso(chunk.updated_at),
    }


def extract_exact_terms(content: str) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for term, pattern in EXACT_TERM_PATTERNS:
        matched = pattern.search(content) is not None if pattern is not None else term in content
        if matched and term not in seen:
            results.append(term)
            seen.add(term)
    return results


def get_syncable_chunks_query(
    db: Session,
    document_id: UUID | None = None,
    *,
    settings: Any | None = None,
) -> Select[tuple[Document, DocumentChunk]]:
    del db
    settings = settings or get_settings()
    embedding_dim = int(settings.embedding_dim)
    embedding_model = str(settings.embedding_model)
    statement = (
        select(Document, DocumentChunk)
        .join(DocumentChunk, DocumentChunk.document_id == Document.id)
        .where(
            DocumentChunk.embedding_status == EMBEDDING_STATUS_EMBEDDED,
            DocumentChunk.embedding.is_not(None),
            DocumentChunk.embedding_dim == embedding_dim,
            DocumentChunk.embedding_model == embedding_model,
        )
        .order_by(Document.id, DocumentChunk.chunk_index)
    )
    if document_id is not None:
        statement = statement.where(Document.id == document_id)
    return statement


def count_postgres_syncable_chunks(
    db: Session,
    *,
    settings: Any,
    document_id: UUID | None = None,
) -> int:
    embedding_dim = int(settings.embedding_dim)
    embedding_model = str(settings.embedding_model)
    statement = (
        select(func.count())
        .select_from(DocumentChunk)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.embedding_status == EMBEDDING_STATUS_EMBEDDED,
            DocumentChunk.embedding.is_not(None),
            DocumentChunk.embedding_dim == embedding_dim,
            DocumentChunk.embedding_model == embedding_model,
        )
    )
    if document_id is not None:
        statement = statement.where(Document.id == document_id)
    return int(db.execute(statement).scalar_one())


def is_syncable_chunk(chunk: Any, settings: Any) -> bool:
    return (
        getattr(chunk, "embedding_status", None) == EMBEDDING_STATUS_EMBEDDED
        and getattr(chunk, "embedding", None) is not None
        and getattr(chunk, "embedding_dim", None) == int(settings.embedding_dim)
        and getattr(chunk, "embedding_model", None) == str(settings.embedding_model)
    )


def _load_syncable_chunks(
    db: Session,
    *,
    settings: Any,
    document_id: UUID | None,
) -> list[SyncableChunkRow]:
    rows = db.execute(
        get_syncable_chunks_query(db, document_id=document_id, settings=settings)
    ).all()
    return [
        SyncableChunkRow(document=document, chunk=chunk)
        for document, chunk in rows
        if is_syncable_chunk(chunk, settings)
    ]


def _bulk_index_rows(
    client: SearchEngineClientProtocol,
    *,
    index_name: str,
    rows: list[SyncableChunkRow],
    settings: Any,
    batch_size: int,
) -> tuple[int, int, list[str]]:
    indexed = 0
    failed = 0
    errors: list[str] = []

    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        if not batch:
            continue
        body: list[dict[str, Any]] = []
        for row in batch:
            chunk_id = str(row.chunk.id)
            body.append({"index": {"_index": index_name, "_id": chunk_id}})
            body.append(build_chunk_index_payload(row.document, row.chunk, settings))

        try:
            response = client.bulk(body=body, refresh=True)
        except Exception as exc:
            raise BusinessError(
                SEARCH_INDEX_REBUILD_FAILED,
                "Failed to bulk index chunks.",
                detail=_error_detail(exc),
                status_code=500,
            ) from exc

        batch_indexed, batch_failed, batch_errors = _parse_bulk_response(response)
        indexed += batch_indexed
        failed += batch_failed
        errors.extend(batch_errors)

        if batch_failed:
            raise BusinessError(
                SEARCH_INDEX_REBUILD_FAILED,
                "OpenSearch bulk indexing partially failed.",
                detail={
                    "indexed": indexed,
                    "failed": failed,
                    "errors": errors[:50],
                },
                status_code=500,
            )

    return indexed, failed, errors


def _parse_bulk_response(response: dict[str, Any]) -> tuple[int, int, list[str]]:
    items = response.get("items", [])
    if not response.get("errors"):
        return len(items), 0, []

    indexed = 0
    failed = 0
    errors: list[str] = []
    for item in items:
        operation = item.get("index") or item.get("create") or item.get("update") or {}
        status_code = int(operation.get("status", 0))
        if 200 <= status_code < 300:
            indexed += 1
        else:
            failed += 1
            errors.append(_format_bulk_item_error(operation))
    return indexed, failed, errors


def _format_bulk_item_error(operation: dict[str, Any]) -> str:
    chunk_id = str(operation.get("_id") or "unknown")[:64]
    status_code = int(operation.get("status", 0))
    error = operation.get("error")
    if not isinstance(error, dict):
        return f"chunk_id={chunk_id} status={status_code}"

    error_type = str(error.get("type") or "unknown")[:80]
    reason = str(error.get("reason") or "")[:200]
    return (
        f"chunk_id={chunk_id} status={status_code} "
        f"error_type={error_type} reason={reason}"
    ).strip()


def _delete_document_from_index(
    client: SearchEngineClientProtocol,
    *,
    index_name: str,
    document_id: UUID | None,
) -> int:
    if document_id is None:
        raise BusinessError(
            SEARCH_ENGINE_CONFIG_INVALID,
            "document_id is required when rebuilding a single document.",
            status_code=400,
        )
    try:
        response = client.delete_by_query(
            index=index_name,
            body={"query": {"term": {"document_id": str(document_id)}}},
            refresh=True,
            conflicts="proceed",
        )
    except Exception as exc:
        raise BusinessError(
            SEARCH_INDEX_REBUILD_FAILED,
            "Failed to delete old document chunks from search index.",
            detail=_error_detail(exc),
            status_code=500,
        ) from exc
    return int(response.get("deleted", 0))


def _ensure_alias(client: SearchEngineClientProtocol, *, index_name: str, alias: str) -> bool:
    try:
        alias_exists = bool(client.indices.exists_alias(name=alias))
    except Exception as exc:
        raise BusinessError(
            SEARCH_ENGINE_UNAVAILABLE,
            "Search engine is unavailable while checking alias.",
            detail=_error_detail(exc),
            status_code=503,
        ) from exc
    if alias_exists:
        return False
    try:
        client.indices.put_alias(index=index_name, name=alias)
    except Exception as exc:
        raise BusinessError(
            SEARCH_INDEX_CREATE_FAILED,
            "Failed to create search index alias.",
            detail=_error_detail(exc),
            status_code=500,
        ) from exc
    return True


def _mapping_matches_target(current_mapping: dict[str, Any], target_mapping: dict[str, Any], *, index_name: str) -> bool:
    current_props = _extract_mapping_properties(current_mapping, index_name=index_name)
    target_props = target_mapping["properties"]

    checks = [
        current_props.get("content", {}).get("type") == target_props["content"]["type"],
        current_props.get("exact_terms", {}).get("type") == target_props["exact_terms"]["type"],
        current_props.get("source_metadata", {}).get("enabled") is False,
        current_props.get("embedding", {}).get("type") == target_props["embedding"]["type"],
        current_props.get("embedding", {}).get("dimension") == target_props["embedding"]["dimension"],
        current_props.get("embedding", {}).get("space_type") == target_props["embedding"]["space_type"],
    ]
    return all(checks)


def _extract_mapping_properties(mapping_response: dict[str, Any], *, index_name: str) -> dict[str, Any]:
    if index_name in mapping_response:
        return mapping_response[index_name].get("mappings", {}).get("properties", {})
    return mapping_response.get("mappings", {}).get("properties", mapping_response.get("properties", {}))


def _validate_rebuild_scope(scope: str, document_id: UUID | None) -> str:
    normalized_scope = scope.strip().lower()
    if normalized_scope not in {REBUILD_SCOPE_ALL, REBUILD_SCOPE_DOCUMENT}:
        raise BusinessError(
            SEARCH_ENGINE_CONFIG_INVALID,
            "Invalid search index rebuild scope.",
            detail={"scope": scope, "allowed_scopes": [REBUILD_SCOPE_ALL, REBUILD_SCOPE_DOCUMENT]},
            status_code=400,
        )
    if normalized_scope == REBUILD_SCOPE_DOCUMENT and document_id is None:
        raise BusinessError(
            SEARCH_ENGINE_CONFIG_INVALID,
            "document_id is required when scope is document.",
            status_code=400,
        )
    return normalized_scope


def _embedding_to_float_list(value: Any) -> list[float]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [float(item) for item in value]


def _datetime_to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _error_detail(exc: Exception) -> dict[str, str]:
    return {"error_type": exc.__class__.__name__}


def _format_error(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {exc}"
