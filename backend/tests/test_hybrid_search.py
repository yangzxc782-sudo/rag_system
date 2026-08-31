from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.api.v1 import search as search_api
from app.core.errors import (
    HYBRID_SEARCH_CONFIG_INVALID,
    HYBRID_SEARCH_FAILED,
    SEARCH_ENGINE_UNAVAILABLE,
    SEARCH_QUERY_EMPTY,
    BusinessError,
)
from app.retrieval.embeddings import EmbeddingResult
from app.schemas.search import SearchRequest
from app.services import hybrid_search
from app.services.hybrid_search import HybridSearchItem, HybridSearchResult, SearchEngineHit


def make_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "search_index_alias": "casting_chunks_current",
        "search_query_analyzer": "ik_smart",
        "embedding_model": "Qwen3-Embedding-0.6B",
        "embedding_dim": 1024,
        "hybrid_keyword_weight": 0.5,
        "hybrid_vector_weight": 0.5,
        "hybrid_rrf_k": 60,
        "hybrid_keyword_top_k": 50,
        "hybrid_vector_top_k": 50,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeEmbeddingProvider:
    def __init__(self, embedding: list[float] | None = None) -> None:
        self.embedding = embedding or [0.1 for _ in range(1024)]
        self.encode_query_calls: list[str] = []
        self.encode_documents_calls: list[list[str]] = []

    def encode_query(self, query: str) -> EmbeddingResult:
        self.encode_query_calls.append(query)
        return EmbeddingResult(
            embeddings=[self.embedding],
            embedding_model="Qwen3-Embedding-0.6B",
            embedding_dim=len(self.embedding),
            device="cpu",
            provider="fake",
        )

    def encode_documents(self, texts: list[str]) -> EmbeddingResult:
        self.encode_documents_calls.append(texts)
        raise AssertionError("encode_documents must not be called for query embedding")


class FakeSearchClient:
    def __init__(self, responses: list[dict] | None = None, error: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.search_calls: list[dict[str, object]] = []

    def search(self, **kwargs: object) -> dict:
        self.search_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        return {"hits": {"hits": []}}


class TrackedFilterSession:
    def __init__(self, *allowed: str, events: list[str] | None = None) -> None:
        self.allowed = [UUID(value) for value in allowed]
        self.events = events if events is not None else []
        self.closed = False

    def scalars(self, _statement: object) -> SimpleNamespace:
        self.events.append("filter-select")
        return SimpleNamespace(all=lambda: self.allowed)

    def close(self) -> None:
        self.closed = True
        self.events.append("filter-close")


def make_source(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "chunk_id": str(uuid4()),
        "document_id": str(uuid4()),
        "original_filename": "casting.md",
        "chunk_index": 1,
        "content": "冒口设计应保证热节区域补缩，并避免缩孔。",
        "source_metadata": {"page_start": 1},
        "exact_terms": ["冒口", "热节", "补缩", "缩孔"],
        "embedding_model": "Qwen3-Embedding-0.6B",
        "embedding_dim": 1024,
    }
    values.update(overrides)
    return values


def make_response(*hits: dict[str, object]) -> dict:
    return {"hits": {"hits": list(hits)}}


def make_hit(source: dict[str, object], score: float) -> dict[str, object]:
    return {"_id": source["chunk_id"], "_score": score, "_source": source}


def test_query_empty_raises_search_query_empty() -> None:
    with pytest.raises(BusinessError) as exc_info:
        hybrid_search.hybrid_search_chunks(
            SimpleNamespace(),
            query="   ",
            client=FakeSearchClient(),
            settings=make_settings(),
            embedding_provider=FakeEmbeddingProvider(),
        )

    assert exc_info.value.code == SEARCH_QUERY_EMPTY


def test_hybrid_search_uses_encode_query_not_encode_documents() -> None:
    provider = FakeEmbeddingProvider()
    client = FakeSearchClient([make_response(), make_response()])

    hybrid_search.hybrid_search_chunks(
        SimpleNamespace(),
        query="冒口补缩",
        client=client,
        settings=make_settings(),
        embedding_provider=provider,
    )

    assert provider.encode_query_calls == ["冒口补缩"]
    assert provider.encode_documents_calls == []


def test_keyword_search_body_contains_ik_content_query_and_filters() -> None:
    document_id = uuid4()
    body = hybrid_search.build_keyword_search_body("冒口补缩", make_settings(), document_id=document_id)

    multi_match = body["query"]["bool"]["must"][0]["multi_match"]
    filters = body["query"]["bool"]["filter"]
    assert multi_match["query"] == "冒口补缩"
    assert "content^3" in multi_match["fields"]
    assert multi_match["analyzer"] == "ik_smart"
    assert {"term": {"document_id": str(document_id)}} in filters
    assert {"term": {"embedding_status": "embedded"}} in filters
    assert {"term": {"embedding_dim": 1024}} in filters
    assert {"term": {"embedding_model": "Qwen3-Embedding-0.6B"}} in filters


def test_vector_search_body_contains_knn_embedding_query_and_filters() -> None:
    document_id = uuid4()
    body = hybrid_search.build_vector_search_body([0.1, 0.2], make_settings(), document_id=document_id)

    knn = body["query"]["knn"]["embedding"]
    filters = knn["filter"]["bool"]["filter"]
    assert knn["vector"] == [0.1, 0.2]
    assert knn["k"] == 50
    assert {"term": {"document_id": str(document_id)}} in filters
    assert {"term": {"embedding_status": "embedded"}} in filters


def test_fuse_deduplicates_and_marks_both_keyword_and_vector() -> None:
    source = make_source()
    keyword_hits = [SearchEngineHit(chunk_id=source["chunk_id"], score=12.0, source=source)]
    vector_hits = [SearchEngineHit(chunk_id=source["chunk_id"], score=0.88, source=source)]

    items = hybrid_search.fuse_hybrid_results(keyword_hits, vector_hits, make_settings(), limit=10, query="冒口热节补缩")

    assert len(items) == 1
    assert items[0].retrieval_source == "both"
    assert items[0].keyword_rank == 1
    assert items[0].vector_rank == 1
    assert items[0].keyword_score == 12.0
    assert items[0].vector_score == 0.88


def test_fuse_marks_keyword_and_vector_only_sources() -> None:
    keyword_source = make_source(content="冒口补缩", exact_terms=["冒口", "补缩"])
    vector_source = make_source(content="热节补缩", exact_terms=["热节", "补缩"])

    items = hybrid_search.fuse_hybrid_results(
        [SearchEngineHit(chunk_id=keyword_source["chunk_id"], score=10.0, source=keyword_source)],
        [SearchEngineHit(chunk_id=vector_source["chunk_id"], score=0.9, source=vector_source)],
        make_settings(),
        limit=10,
        query="冒口热节补缩",
    )

    sources = {item.chunk_id: item.retrieval_source for item in items}
    assert sources[str(keyword_source["chunk_id"])] == "keyword"
    assert sources[str(vector_source["chunk_id"])] == "vector"


def test_weighted_rrf_formula_does_not_sum_raw_scores() -> None:
    settings = make_settings(hybrid_keyword_weight=0.5, hybrid_vector_weight=0.5, hybrid_rrf_k=60)

    score = hybrid_search.calculate_weighted_rrf(1, 2, settings)

    assert score == pytest.approx(0.5 / 61 + 0.5 / 62)
    assert score != pytest.approx(12.0 + 0.88)


def test_weighted_rrf_keyword_only_and_vector_only_weights() -> None:
    keyword_only = make_settings(hybrid_keyword_weight=1.0, hybrid_vector_weight=0.0)
    vector_only = make_settings(hybrid_keyword_weight=0.0, hybrid_vector_weight=1.0)

    assert hybrid_search.calculate_weighted_rrf(1, 1, keyword_only) == pytest.approx(1.0 / 61)
    assert hybrid_search.calculate_weighted_rrf(1, 1, vector_only) == pytest.approx(1.0 / 61)


def test_invalid_weights_and_rrf_config_raise_config_error() -> None:
    with pytest.raises(BusinessError) as exc_info:
        hybrid_search.calculate_weighted_rrf(1, 1, make_settings(hybrid_keyword_weight=0, hybrid_vector_weight=0))
    assert exc_info.value.code == HYBRID_SEARCH_CONFIG_INVALID

    with pytest.raises(BusinessError) as exc_info:
        hybrid_search.calculate_weighted_rrf(1, 1, make_settings(hybrid_rrf_k=0))
    assert exc_info.value.code == HYBRID_SEARCH_CONFIG_INVALID


def test_top_k_less_than_limit_raises_config_error() -> None:
    with pytest.raises(BusinessError) as exc_info:
        hybrid_search.hybrid_search_chunks(
            SimpleNamespace(),
            query="冒口",
            limit=10,
            client=FakeSearchClient(),
            settings=make_settings(hybrid_keyword_top_k=5),
            embedding_provider=FakeEmbeddingProvider(),
        )

    assert exc_info.value.code == HYBRID_SEARCH_CONFIG_INVALID


def test_matched_keywords_are_simple_local_matches() -> None:
    matches = hybrid_search.extract_matched_keywords(
        "冒口如何保证热节补缩",
        "冒口设计应保证热节区域补缩。",
        ["冒口", "热节", "补缩"],
    )

    assert matches == ["冒口", "热节", "补缩"]


def test_empty_index_returns_empty_items() -> None:
    result = hybrid_search.hybrid_search_chunks(
        SimpleNamespace(),
        query="冒口",
        client=FakeSearchClient([make_response(), make_response()]),
        settings=make_settings(),
        embedding_provider=FakeEmbeddingProvider(),
    )

    assert result.total == 0
    assert result.items == []


def test_search_engine_unavailable_raises_unavailable() -> None:
    with pytest.raises(BusinessError) as exc_info:
        hybrid_search.hybrid_search_chunks(
            SimpleNamespace(),
            query="冒口",
            client=FakeSearchClient(error=ConnectionError("no service")),
            settings=make_settings(),
            embedding_provider=FakeEmbeddingProvider(),
        )

    assert exc_info.value.code == SEARCH_ENGINE_UNAVAILABLE


def test_opensearch_query_exception_raises_hybrid_search_failed() -> None:
    with pytest.raises(BusinessError) as exc_info:
        hybrid_search.hybrid_search_chunks(
            SimpleNamespace(),
            query="冒口",
            client=FakeSearchClient(error=RuntimeError("query failed")),
            settings=make_settings(),
            embedding_provider=FakeEmbeddingProvider(),
        )

    assert exc_info.value.code == HYBRID_SEARCH_FAILED


def test_hybrid_search_calls_alias_and_returns_ranked_items() -> None:
    source = make_source()
    client = FakeSearchClient([make_response(make_hit(source, 10.0)), make_response(make_hit(source, 0.9))])
    filter_session = TrackedFilterSession(str(source["document_id"]))

    result = hybrid_search.hybrid_search_chunks(
        SimpleNamespace(),
        query="冒口热节补缩",
        client=client,
        settings=make_settings(),
        embedding_provider=FakeEmbeddingProvider(),
        deletion_filter_session_factory=lambda: filter_session,
    )

    assert [call["index"] for call in client.search_calls] == ["casting_chunks_current", "casting_chunks_current"]
    assert result.total == 1
    assert result.items[0].hybrid_score > 0
    assert filter_session.closed is True


def test_hybrid_filters_stale_deleting_document_before_rrf() -> None:
    deleting_source = make_source()
    normal_source = make_source()
    client = FakeSearchClient(
        [
            make_response(
                make_hit(deleting_source, 20.0),
                make_hit(normal_source, 10.0),
            ),
            make_response(make_hit(deleting_source, 0.99)),
        ]
    )
    filter_session = TrackedFilterSession(str(normal_source["document_id"]))

    result = hybrid_search.hybrid_search_chunks(
        SimpleNamespace(),
        query="冒口",
        client=client,
        settings=make_settings(),
        embedding_provider=FakeEmbeddingProvider(),
        deletion_filter_session_factory=lambda: filter_session,
    )

    assert [item.document_id for item in result.items] == [
        str(normal_source["document_id"])
    ]


def test_hybrid_filter_uses_short_session_closed_before_rrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source()
    events: list[str] = []
    filter_session = TrackedFilterSession(
        str(source["document_id"]),
        events=events,
    )
    request_session = SimpleNamespace(
        scalars=lambda _statement: pytest.fail(
            "request-scoped Session must not filter deletion status"
        )
    )
    client = FakeSearchClient(
        [
            make_response(make_hit(source, 10.0)),
            make_response(make_hit(source, 0.9)),
        ]
    )
    original_fuse = hybrid_search.fuse_hybrid_results

    def tracked_fuse(*args: object, **kwargs: object):
        assert filter_session.closed is True
        events.append("rrf")
        return original_fuse(*args, **kwargs)

    monkeypatch.setattr(
        hybrid_search,
        "SessionLocal",
        lambda: (events.append("filter-open"), filter_session)[1],
        raising=False,
    )
    monkeypatch.setattr(hybrid_search, "fuse_hybrid_results", tracked_fuse)

    result = hybrid_search.hybrid_search_chunks(
        request_session,
        query="冒口",
        client=client,
        settings=make_settings(),
        embedding_provider=FakeEmbeddingProvider(),
    )

    assert result.total == 1
    assert events == ["filter-open", "filter-select", "filter-close", "rrf"]


def test_api_post_search_success(monkeypatch: pytest.MonkeyPatch) -> None:
    item = HybridSearchItem(
        chunk_id=str(uuid4()),
        document_id=str(uuid4()),
        original_filename="casting.md",
        chunk_index=1,
        content="冒口补缩",
        source_metadata={"page_start": 1},
        retrieval_source="both",
        keyword_score=12.0,
        vector_score=0.9,
        keyword_rank=1,
        vector_rank=1,
        hybrid_score=0.016,
        matched_keywords=["冒口", "补缩"],
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_dim=1024,
    )
    monkeypatch.setattr(
        search_api.hybrid_search_service,
        "hybrid_search_chunks",
        lambda db, query, limit, document_id: HybridSearchResult(query=query, limit=limit, total=1, items=[item]),
    )

    response = search_api.hybrid_search_endpoint(SearchRequest(query="冒口补缩"), SimpleNamespace())

    assert response.success is True
    assert response.data.items[0].hybrid_score == pytest.approx(0.016)
    assert response.data.items[0].retrieval_source == "both"
    assert response.data.items[0].keyword_score == pytest.approx(12.0)
    assert response.data.items[0].vector_score == pytest.approx(0.9)
    assert response.data.items[0].matched_keywords == ["冒口", "补缩"]


def test_api_post_search_query_empty_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_empty(*args: object, **kwargs: object) -> None:
        raise BusinessError(SEARCH_QUERY_EMPTY, "Search query must not be empty.", status_code=400)

    monkeypatch.setattr(search_api.hybrid_search_service, "hybrid_search_chunks", raise_empty)

    response = search_api.hybrid_search_endpoint(SearchRequest(query=" "), SimpleNamespace())

    assert response.status_code == 400


def test_api_post_search_unavailable_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_unavailable(*args: object, **kwargs: object) -> None:
        raise BusinessError(SEARCH_ENGINE_UNAVAILABLE, "Search engine is unavailable.", status_code=503)

    monkeypatch.setattr(search_api.hybrid_search_service, "hybrid_search_chunks", raise_unavailable)

    response = search_api.hybrid_search_endpoint(SearchRequest(query="冒口"), SimpleNamespace())

    assert response.status_code == 503


def test_existing_vector_and_index_api_endpoints_remain() -> None:
    assert hasattr(search_api, "vector_search_endpoint")
    assert hasattr(search_api, "create_search_index_endpoint")
    assert hasattr(search_api, "rebuild_search_index_endpoint")
    assert hasattr(search_api, "search_index_status_endpoint")
