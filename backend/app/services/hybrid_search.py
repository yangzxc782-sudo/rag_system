from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_GENERATION_FAILED,
    HYBRID_SEARCH_CONFIG_INVALID,
    HYBRID_SEARCH_FAILED,
    SEARCH_ENGINE_UNAVAILABLE,
    SEARCH_INDEX_NOT_FOUND,
    SEARCH_QUERY_EMPTY,
    BusinessError,
)
from app.models.document import Document
from app.db.session import SessionLocal
from app.retrieval.embeddings import EmbeddingResult, EmbeddingProvider, get_embedding_provider
from app.search_engine.client import SearchEngineClientProtocol, get_search_engine_client
from app.search_engine.index_schema import get_index_alias
from app.services.search_index import DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL, EXACT_TERM_PATTERNS
from app.services.retrieval_admission import VERSION_FIELDS, STRUCTURE_FIELDS, published_targets, filter_published_hits
from app.services.embedding_contract import embedding_fingerprint


MAX_HYBRID_SEARCH_LIMIT = 50
EMBEDDING_STATUS_EMBEDDED = "embedded"
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_\-.]*|[\u4e00-\u9fff]{2,}")


@dataclass(frozen=True)
class SearchEngineHit:
    chunk_id: str
    score: float
    source: dict[str, Any]
    index_name: str | None = None


@dataclass(frozen=True)
class HybridSearchItem:
    chunk_id: str
    document_id: str
    original_filename: str
    chunk_index: int
    content: str
    source_metadata: dict[str, Any] | None
    retrieval_source: str
    keyword_score: float | None
    vector_score: float | None
    keyword_rank: int | None
    vector_rank: int | None
    hybrid_score: float
    matched_keywords: list[str]
    embedding_model: str | None
    embedding_dim: int | None
    source_version: str | None = None
    graph_build_id: str | None = None
    chunk_set_id: str | None = None
    source_start: int | None = None
    source_end: int | None = None
    content_sha256: str | None = None
    embedding_fingerprint: str | None = None


@dataclass(frozen=True)
class HybridSearchResult:
    query: str
    limit: int
    total: int
    items: list[HybridSearchItem]


def hybrid_search_chunks(
    db: Session,
    *,
    query: str,
    limit: int = 10,
    document_id: UUID | None = None,
    client: SearchEngineClientProtocol | None = None,
    settings: Any | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    deletion_filter_session_factory: Callable[[], Session] | None = None,
) -> HybridSearchResult:
    settings = settings or get_settings()
    normalized_query = query.strip()
    if not normalized_query:
        raise BusinessError(SEARCH_QUERY_EMPTY, "Search query must not be empty.", status_code=400)

    normalized_limit = _validate_hybrid_config(settings, limit)
    targets = None
    filter_factory = deletion_filter_session_factory or SessionLocal
    if getattr(settings, "pdf_kg_search_enabled", False):
        targets = published_targets(filter_factory, settings, document_id)
        if not targets:
            return HybridSearchResult(query=normalized_query, limit=normalized_limit, total=0, items=[])
    provider = embedding_provider or get_embedding_provider(settings)
    embedding_result = _encode_query_embedding(provider, normalized_query)
    query_embedding = _extract_query_embedding(
        embedding_result,
        expected_dim=int(getattr(settings, "embedding_dim", DEFAULT_EMBEDDING_DIM)),
        expected_model=str(getattr(settings, "embedding_model", DEFAULT_EMBEDDING_MODEL)),
    )

    client = client or get_search_engine_client(settings)
    alias = get_index_alias(settings)
    keyword_body = build_keyword_search_body(normalized_query, settings, document_id=document_id)
    vector_body = build_vector_search_body(query_embedding, settings, document_id=document_id)
    if targets is not None:
        alias = list(targets.values())
        filters = [{"term": {"schema_version": 2}}, {"terms": {"chunk_set_id": list(targets)}},
                   {"term": {"embedding_fingerprint": embedding_fingerprint(settings)}}]
        keyword_body["query"]["bool"]["filter"].extend(filters)
        vector_body["query"]["knn"]["embedding"]["filter"]["bool"]["filter"].extend(filters)

    try:
        keyword_response = client.search(index=alias, body=keyword_body)
        vector_response = client.search(index=alias, body=vector_body)
    except Exception as exc:
        _raise_search_error(exc)

    keyword_hits = _parse_search_hits(keyword_response)
    vector_hits = _parse_search_hits(vector_response)
    if targets is not None:
        keyword_hits, vector_hits = filter_published_hits(keyword_hits, vector_hits, filter_factory, settings, targets)
    else:
        keyword_hits, vector_hits = _filter_normal_document_hits(keyword_hits, vector_hits, session_factory=filter_factory)
    items = fuse_hybrid_results(keyword_hits, vector_hits, settings, limit=normalized_limit, query=normalized_query)
    return HybridSearchResult(query=normalized_query, limit=normalized_limit, total=len(items), items=items)


def _filter_normal_document_hits(
    keyword_hits: list[SearchEngineHit],
    vector_hits: list[SearchEngineHit],
    *,
    session_factory: Callable[[], Session],
) -> tuple[list[SearchEngineHit], list[SearchEngineHit]]:
    document_ids: set[UUID] = set()
    for hit in (*keyword_hits, *vector_hits):
        try:
            document_ids.add(UUID(str(hit.source.get("document_id") or "")))
        except (TypeError, ValueError):
            continue
    if not document_ids:
        return [], []
    filter_db = session_factory()
    try:
        allowed = set(
            filter_db.scalars(
                select(Document.id).where(
                    Document.id.in_(document_ids),
                    Document.deletion_status == "normal",
                )
            ).all()
        )
    finally:
        filter_db.close()

    def keep(hit: SearchEngineHit) -> bool:
        try:
            return UUID(str(hit.source.get("document_id") or "")) in allowed
        except (TypeError, ValueError):
            return False

    return (
        [hit for hit in keyword_hits if keep(hit)],
        [hit for hit in vector_hits if keep(hit)],
    )


def build_keyword_search_body(query: str, settings: Any, document_id: UUID | None = None) -> dict[str, Any]:
    return {
        "size": int(settings.hybrid_keyword_top_k),
        "_source": _source_fields(),
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["content^3", "content_max^2", "content_smart"],
                            "analyzer": settings.search_query_analyzer,
                        }
                    }
                ],
                "filter": _common_filters(settings, document_id=document_id),
            }
        },
    }


def build_vector_search_body(
    query_embedding: list[float],
    settings: Any,
    document_id: UUID | None = None,
) -> dict[str, Any]:
    return {
        "size": int(settings.hybrid_vector_top_k),
        "_source": _source_fields(),
        "query": {
            "knn": {
                "embedding": {
                    "vector": query_embedding,
                    "k": int(settings.hybrid_vector_top_k),
                    "filter": {
                        "bool": {
                            "filter": _common_filters(settings, document_id=document_id),
                        }
                    },
                }
            }
        },
    }


def fuse_hybrid_results(
    keyword_hits: list[SearchEngineHit],
    vector_hits: list[SearchEngineHit],
    settings: Any,
    *,
    limit: int,
    query: str = "",
) -> list[HybridSearchItem]:
    entries: dict[str, dict[str, Any]] = {}

    for rank, hit in enumerate(keyword_hits, start=1):
        entry = entries.setdefault(hit.chunk_id, {"source": hit.source})
        entry["source"] = hit.source or entry["source"]
        entry["keyword_score"] = hit.score
        entry["keyword_rank"] = rank

    for rank, hit in enumerate(vector_hits, start=1):
        entry = entries.setdefault(hit.chunk_id, {"source": hit.source})
        entry["source"] = entry.get("source") or hit.source
        entry["vector_score"] = hit.score
        entry["vector_rank"] = rank

    items: list[HybridSearchItem] = []
    for chunk_id, entry in entries.items():
        keyword_rank = entry.get("keyword_rank")
        vector_rank = entry.get("vector_rank")
        hybrid_score = calculate_weighted_rrf(keyword_rank, vector_rank, settings)
        source = entry.get("source") or {}
        content = str(source.get("content") or "")
        exact_terms = source.get("exact_terms")
        retrieval_source = _retrieval_source(keyword_rank, vector_rank)
        items.append(
            HybridSearchItem(
                chunk_id=str(source.get("chunk_id") or chunk_id),
                document_id=str(source.get("document_id") or ""),
                original_filename=str(source.get("original_filename") or ""),
                chunk_index=int(source.get("chunk_index") or 0),
                content=content,
                source_metadata=source.get("source_metadata"),
                retrieval_source=retrieval_source,
                keyword_score=entry.get("keyword_score"),
                vector_score=entry.get("vector_score"),
                keyword_rank=keyword_rank,
                vector_rank=vector_rank,
                hybrid_score=hybrid_score,
                matched_keywords=extract_matched_keywords(query, content, exact_terms),
                embedding_model=source.get("embedding_model"),
                embedding_dim=source.get("embedding_dim"),
                **{key: source.get(key) for key in VERSION_FIELDS},
            )
        )

    items.sort(
        key=lambda item: (
            item.hybrid_score,
            -(item.keyword_rank or 10**9),
            -(item.vector_rank or 10**9),
        ),
        reverse=True,
    )
    return items[:limit]


def calculate_weighted_rrf(keyword_rank: int | None, vector_rank: int | None, settings: Any) -> float:
    _validate_rrf_settings(settings, limit=1)
    rrf_k = float(settings.hybrid_rrf_k)
    score = 0.0
    if keyword_rank is not None:
        score += float(settings.hybrid_keyword_weight) / (rrf_k + keyword_rank)
    if vector_rank is not None:
        score += float(settings.hybrid_vector_weight) / (rrf_k + vector_rank)
    return score


def extract_matched_keywords(query: str, content: str, exact_terms: list[str] | None = None) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()

    query_lower = query.lower()
    content_lower = content.lower()
    candidate_terms = [term for term, _pattern in EXACT_TERM_PATTERNS]
    if exact_terms:
        candidate_terms.extend(str(term) for term in exact_terms)

    for term in candidate_terms:
        normalized = term.lower()
        if normalized and normalized in query_lower and normalized in content_lower and term not in seen:
            results.append(term)
            seen.add(term)

    for token in _TOKEN_PATTERN.findall(query):
        normalized = token.lower()
        if len(normalized) < 2 or token in seen:
            continue
        if normalized in content_lower:
            results.append(token)
            seen.add(token)

    return results


def _validate_hybrid_config(settings: Any, limit: int) -> int:
    normalized_limit = int(limit)
    if normalized_limit <= 0 or normalized_limit > MAX_HYBRID_SEARCH_LIMIT:
        raise BusinessError(
            HYBRID_SEARCH_CONFIG_INVALID,
            "Hybrid search limit must be between 1 and 50.",
            detail={"limit": normalized_limit, "max_limit": MAX_HYBRID_SEARCH_LIMIT},
            status_code=400,
        )
    _validate_rrf_settings(settings, limit=normalized_limit)
    return normalized_limit


def _validate_rrf_settings(settings: Any, *, limit: int) -> None:
    keyword_weight = float(settings.hybrid_keyword_weight)
    vector_weight = float(settings.hybrid_vector_weight)
    if keyword_weight == 0 and vector_weight == 0:
        raise BusinessError(
            HYBRID_SEARCH_CONFIG_INVALID,
            "hybrid_keyword_weight and hybrid_vector_weight cannot both be 0.",
            status_code=400,
        )
    if int(settings.hybrid_rrf_k) <= 0:
        raise BusinessError(HYBRID_SEARCH_CONFIG_INVALID, "hybrid_rrf_k must be greater than 0.", status_code=400)
    if int(settings.hybrid_keyword_top_k) < limit:
        raise BusinessError(
            HYBRID_SEARCH_CONFIG_INVALID,
            "hybrid_keyword_top_k must be greater than or equal to limit.",
            detail={"hybrid_keyword_top_k": int(settings.hybrid_keyword_top_k), "limit": limit},
            status_code=400,
        )
    if int(settings.hybrid_vector_top_k) < limit:
        raise BusinessError(
            HYBRID_SEARCH_CONFIG_INVALID,
            "hybrid_vector_top_k must be greater than or equal to limit.",
            detail={"hybrid_vector_top_k": int(settings.hybrid_vector_top_k), "limit": limit},
            status_code=400,
        )


def _encode_query_embedding(provider: EmbeddingProvider, query: str) -> EmbeddingResult:
    try:
        return provider.encode_query(query)
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            EMBEDDING_GENERATION_FAILED,
            "Query embedding generation failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc


def _extract_query_embedding(result: EmbeddingResult, *, expected_dim: int, expected_model: str) -> list[float]:
    if result.embedding_dim != expected_dim:
        raise BusinessError(
            EMBEDDING_DIMENSION_MISMATCH,
            "Query embedding dimension does not match search index dimension.",
            detail={"expected_dim": expected_dim, "actual_dim": result.embedding_dim},
            status_code=500,
        )
    if result.embedding_model != expected_model:
        raise BusinessError(
            EMBEDDING_DIMENSION_MISMATCH,
            "Query embedding model does not match search index model.",
            detail={"expected_model": expected_model, "actual_model": result.embedding_model},
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
    if len(embedding) != expected_dim:
        raise BusinessError(
            EMBEDDING_DIMENSION_MISMATCH,
            "Query embedding vector dimension does not match search index dimension.",
            detail={"expected_dim": expected_dim, "actual_dim": len(embedding)},
            status_code=500,
        )
    return [float(value) for value in embedding]


def _parse_search_hits(response: dict[str, Any]) -> list[SearchEngineHit]:
    hits = response.get("hits", {}).get("hits", [])
    parsed_hits: list[SearchEngineHit] = []
    for hit in hits:
        source = hit.get("_source") or {}
        chunk_id = str(source.get("chunk_id") or hit.get("_id") or "")
        if not chunk_id:
            continue
        parsed_hits.append(
            SearchEngineHit(
                chunk_id=chunk_id,
                score=float(hit.get("_score") or 0.0),
                source=source,
                index_name=hit.get("_index"),
            )
        )
    return parsed_hits


def _common_filters(settings: Any, *, document_id: UUID | None) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = [
        {"term": {"embedding_status": EMBEDDING_STATUS_EMBEDDED}},
        {"term": {"embedding_dim": int(getattr(settings, "embedding_dim", DEFAULT_EMBEDDING_DIM))}},
        {"term": {"embedding_model": str(getattr(settings, "embedding_model", DEFAULT_EMBEDDING_MODEL))}},
    ]
    if document_id is not None:
        filters.append({"term": {"document_id": str(document_id)}})
    return filters


def _source_fields() -> list[str]:
    return [
        "chunk_id",
        "document_id",
        "original_filename",
        "chunk_index",
        "content",
        "source_metadata",
        *STRUCTURE_FIELDS,
        "exact_terms",
        "embedding_model",
        "embedding_dim", "schema_version", *VERSION_FIELDS,
    ]


def _retrieval_source(keyword_rank: int | None, vector_rank: int | None) -> str:
    if keyword_rank is not None and vector_rank is not None:
        return "both"
    if keyword_rank is not None:
        return "keyword"
    return "vector"


def _raise_search_error(exc: Exception) -> None:
    status_code = getattr(exc, "status_code", None)
    error_text = str(exc).lower()
    error_type = exc.__class__.__name__
    if status_code == 404 or "not_found" in error_text or "not found" in error_text:
        raise BusinessError(
            SEARCH_INDEX_NOT_FOUND,
            "Search index alias was not found.",
            detail={"error_type": error_type},
            status_code=404,
        ) from exc
    if "connection" in error_type.lower() or "timeout" in error_type.lower():
        raise BusinessError(
            SEARCH_ENGINE_UNAVAILABLE,
            "Search engine is unavailable.",
            detail={"error_type": error_type},
            status_code=503,
        ) from exc
    raise BusinessError(
        HYBRID_SEARCH_FAILED,
        "Hybrid search query failed.",
        detail={"error_type": error_type},
        status_code=500,
    ) from exc
