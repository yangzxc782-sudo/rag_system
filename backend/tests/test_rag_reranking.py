"""M2 orchestration contracts: fake scoring and storage, never real BGE/CUDA."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
import logging
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import BusinessError, HYBRID_SEARCH_CONFIG_INVALID, LLM_TIMEOUT, RAG_CONFIG_INVALID
from app.retrieval.reranker import RerankResult, RerankScore
from app.schemas.rag import RagAskData
from app.services import hybrid_search, rag, reranking
from app.services.hybrid_search import HybridSearchItem, HybridSearchResult
from test_hybrid_search import FakeEmbeddingProvider, FakeSearchClient, make_hit, make_response
from test_rag_graph_fusion import graph_service, item as graph_item, prompts
from test_rag_service import FakeLLMProvider


@pytest.fixture(autouse=True)
def no_real_reranker(monkeypatch):
    import app.retrieval.local_cross_encoder as runtime
    reranking.close_reranking_service()
    loader = Mock(side_effect=AssertionError("M2 must not load BGE"))
    monkeypatch.setattr(runtime, "_load_local_model", loader)
    yield
    reranking.close_reranking_service()
    loader.assert_not_called()


def settings(**changes):
    values = dict(
        _env_file=None, reranker_enabled=True, reranker_provider="local_transformers",
        reranker_model="BAAI/bge-reranker-v2-m3", reranker_candidate_limit=32,
        reranker_batch_size=8, reranker_max_length=512, reranker_timeout_seconds=2,
        llm_provider="local", llm_model="fake", rag_context_max_chars=12000,
        graph_retrieval_enabled=False, document_deletion_executor_enabled=False,
    )
    values.update(changes)
    return Settings(**values)


def items(count=4):
    return [HybridSearchItem(**vars(graph_item(n, refs=[{
        "anchor_id": f"A{n}", "graph_id": "G", "anchor_type": "table", "table_ref": "T-P8-1",
    }], content=f"evidence-{n}"))) for n in range(1, count + 1)]


class FakeRerankingService:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure
        self.transform = lambda result: result

    def rerank(self, request):
        self.calls.append(request)
        if isinstance(self.failure, Exception):
            raise self.failure
        if self.failure:
            return RerankResult(request.request_id, failure_reason=self.failure)
        return self.transform(RerankResult(request.request_id, tuple(
            RerankScore(c.chunk_id, c.original_rank, float(c.original_rank))
            for c in reversed(request.candidates)
        )))


def harness(monkeypatch, sources=None, *, config=None, failure=None):
    config = config or settings()
    sources = items() if sources is None else sources
    snapshots = []

    def search(db, *, query, limit, document_id, settings):
        hybrid_search._validate_hybrid_config(settings, limit)
        result = HybridSearchResult(query, limit, len(sources[:limit]), list(sources[:limit]))
        snapshots.append(result)
        return result

    search_spy = Mock(side_effect=search)
    monkeypatch.setattr(hybrid_search, "hybrid_search_chunks", search_spy)
    service = FakeRerankingService(failure)
    getter = Mock(return_value=service)
    monkeypatch.setattr(reranking, "get_reranking_service", getter)
    context_builder = rag.build_rag_context
    orders = []

    def context(*args, **kwargs):
        orders.append(kwargs.get("preserve_order", False))
        return context_builder(*args, **kwargs)

    monkeypatch.setattr(rag, "build_rag_context", context)
    return SimpleNamespace(config=config, sources=sources, snapshots=snapshots,
        hybrid=search_spy, service=service, getter=getter, orders=orders, llm=FakeLLMProvider())


def ask(h, *, k=2, graph=None):
    return rag.answer_question(object(), " question ", limit=k, settings=h.config,
                               llm_provider=h.llm, graph_retrieval=graph)


@pytest.mark.parametrize("enabled,k,c,search_limit,applied,reason", [
    (False, 8, 32, 8, False, "disabled"),
    (True, 8, 32, 32, True, None),
    (True, 8, 8, 8, True, None),
    (True, 32, 16, 32, False, "public_limit_exceeds_reranker_capacity"),
])
def test_k_capacity_decision_one_hybrid(monkeypatch, caplog, enabled, k, c, search_limit, applied, reason):
    h = harness(monkeypatch, items(32), config=settings(reranker_enabled=enabled, reranker_candidate_limit=c))
    with caplog.at_level(logging.INFO, logger="app.services.rag"):
        answer = ask(h, k=k)
    h.hybrid.assert_called_once()
    assert h.hybrid.call_args.kwargs["limit"] == search_limit
    assert h.orders == [applied]
    assert len(h.service.calls) == int(applied)
    assert h.getter.call_count == int(applied)
    expected = list(reversed(h.sources[:c]))[:k] if applied else h.sources[:k]
    assert answer.retrieval.items == expected
    assert answer.retrieval.limit == k and answer.retrieval.total == len(expected)
    if reason:
        assert reason in caplog.text


@pytest.mark.parametrize("field,value", [
    ("reranker_candidate_limit", None), ("reranker_batch_size", None),
    ("reranker_max_length", None), ("reranker_timeout_seconds", None),
    ("reranker_device", ""), ("reranker_dtype", ""), ("reranker_provider", "local_qwen3"),
    ("reranker_model_path", ""),
])
def test_incomplete_configuration_uses_k_before_search(monkeypatch, field, value):
    h = harness(monkeypatch, config=settings(**{field: value}))
    answer = ask(h)
    assert h.hybrid.call_args.kwargs["limit"] == 2
    h.hybrid.assert_called_once()
    h.getter.assert_not_called()
    assert h.orders == [False] and answer.retrieval.items == h.sources[:2]


@pytest.mark.parametrize("changes", [
    {"reranker_candidate_limit": 51},
    {"reranker_candidate_limit": 32, "hybrid_keyword_top_k": 16},
    {"reranker_candidate_limit": 32, "hybrid_vector_top_k": 16},
])
def test_unreachable_capacity_preserves_valid_public_k_baseline(monkeypatch, changes):
    h = harness(monkeypatch, config=settings(**changes))
    answer = ask(h, k=8)
    assert h.hybrid.call_args.kwargs["limit"] == 8
    assert answer.retrieval.items == h.sources and h.orders == [False]
    h.getter.assert_not_called()


@pytest.mark.parametrize("k,code", [(0, RAG_CONFIG_INVALID), (-1, RAG_CONFIG_INVALID),
                                    (51, HYBRID_SEARCH_CONFIG_INVALID)])
def test_invalid_public_k_keeps_existing_error(monkeypatch, k, code):
    h = harness(monkeypatch, config=settings(reranker_candidate_limit=64))
    with pytest.raises(BusinessError) as caught:
        ask(h, k=k)
    assert caught.value.code == code
    assert h.hybrid.call_count == (1 if k == 51 else 0)
    if k == 51:
        assert caught.value.detail["limit"] == 51
    h.getter.assert_not_called()


def test_success_maps_original_objects_and_preserves_all_fields(monkeypatch):
    h = harness(monkeypatch)
    before = deepcopy([asdict(source) for source in h.sources])
    answer = ask(h)
    assert answer.retrieval.query == h.snapshots[0].query == "question"
    assert answer.retrieval.limit == answer.retrieval.total == 2
    assert all(new is old for new, old in zip(answer.retrieval.items, reversed(h.sources)))
    assert [asdict(source) for source in h.sources] == before
    request = h.service.calls[0]
    assert request.query == "question"
    assert [c.original_rank for c in request.candidates] == [1, 2, 3, 4]
    assert [c.content for c in request.candidates] == [source.content for source in h.sources]
    assert set(asdict(request.candidates[0])) == {"chunk_id", "original_rank", "content"}
    assert h.orders == [True]


@pytest.mark.parametrize("failure", ["busy", "timeout", "unavailable", "configuration_invalid",
    "invalid_output", "oom", "inference_exception", "closed", RuntimeError("SECRET"),
    BusinessError("private-code", "SECRET"), "SECRET-invalid-reason"])
def test_failures_keep_exact_snapshot_without_second_hybrid(monkeypatch, caplog, failure):
    h = harness(monkeypatch, failure=failure)
    with caplog.at_level(logging.INFO, logger="app.services.rag"):
        answer = ask(h)
    h.hybrid.assert_called_once()
    assert h.hybrid.call_args.kwargs["limit"] == 32 and len(h.service.calls) == 1
    assert all(a is b for a, b in zip(answer.retrieval.items, h.snapshots[0].items[:2]))
    assert answer.retrieval.limit == answer.retrieval.total == 2
    assert h.orders == [False] and len(h.llm.calls) == 1
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("corruption", ["count", "identity", "rank", "request", "duplicate", "nan", "inf", "partial"])
def test_invalid_success_cannot_apply_partial_order(monkeypatch, corruption):
    h = harness(monkeypatch)

    def corrupt(result):
        scores = list(result.scores)
        if corruption == "count":
            scores.pop()
        elif corruption == "identity":
            scores[0] = replace(scores[0], chunk_id="foreign")
        elif corruption == "rank":
            scores[0] = replace(scores[0], original_rank=99)
        elif corruption == "request":
            return replace(result, request_id="other")
        elif corruption == "duplicate":
            scores[1] = scores[0]
        elif corruption in ("nan", "inf"):
            scores[0] = replace(scores[0], raw_score=float(corruption))
        elif corruption == "partial":
            return replace(result, failure_reason="oom")
        return replace(result, scores=tuple(scores))

    h.service.transform = corrupt
    answer = ask(h)
    assert answer.retrieval.items == h.sources[:2] and h.orders == [False]
    h.hybrid.assert_called_once()


def test_fallback_does_not_resort_even_if_source_list_changes(monkeypatch):
    # The hook cannot normally mutate Hybrid items: it receives only domain DTOs.
    # Deliberately mutate the externally held list to verify an independent order snapshot.
    h = harness(monkeypatch, [items()[1], items()[0], *items()[2:]])
    original = list(h.sources)

    def fail(result):
        h.snapshots[0].items.reverse()
        return replace(result, failure_reason="timeout")

    h.service.transform = fail
    answer = ask(h)
    assert all(a is b for a, b in zip(answer.retrieval.items, original[:2]))
    assert h.orders == [False]
    # The legacy Context builder still applies its original RRF sort on fallback.
    assert [c.chunk_id for c in answer.citations] == [original[1].chunk_id, original[0].chunk_id]


@pytest.mark.parametrize("count", [0, 1, 3])
def test_short_candidate_pool_no_refill_and_empty_no_runtime(monkeypatch, count):
    h = harness(monkeypatch, items(count))
    graph = Mock()
    answer = ask(h, k=8, graph=graph)
    h.hybrid.assert_called_once()
    assert answer.retrieval.total == count and answer.retrieval.limit == 8
    if count:
        assert len(h.service.calls[0].candidates) == count
    else:
        assert answer.context_status == "no_context"
        h.getter.assert_not_called()
        assert not h.llm.calls and h.orders == [False]
        graph.retrieve.assert_not_called()


@pytest.mark.parametrize("char_budget", [12000, 400])
def test_final_text_context_drives_citations_prompt_and_graph(monkeypatch, char_budget):
    sources = items(3)
    sources[2] = replace(sources[2], content="evidence-C " * 300)
    cfg = settings(graph_retrieval_enabled=True, rag_context_max_chars=char_budget)
    h = harness(monkeypatch, sources, config=cfg)
    graph, repo = graph_service(cfg)
    answer = ask(h, k=2, graph=graph)
    retained = sources[2:0:-1] if char_budget == 12000 else [sources[2]]
    assert answer.retrieval.items == [sources[2], sources[1]]
    assert [c.chunk_id for c in answer.citations] == [s.chunk_id for s in retained]
    assert [c.citation_id for c in answer.citations] == list(range(1, len(retained) + 1))
    repo.fetch_table_context.assert_called_once()
    request = repo.fetch_table_context.call_args.args[0]
    assert [p.chunk_id for p in request.provenance] == [s.chunk_id for s in retained]
    assert [p.citation_id for p in request.provenance] == list(range(1, len(retained) + 1))
    assert {ref.anchor_id for ref in request.anchors} == {f"A{s.chunk_index}" for s in retained}
    user_prompt = prompts(h.llm)[1]
    assert f"chunk_id: {sources[2].chunk_id}" in user_prompt
    assert f"chunk_id: {sources[0].chunk_id}" not in user_prompt
    if len(retained) == 2:
        assert user_prompt.index(sources[2].chunk_id) < user_prompt.index(sources[1].chunk_id)
    else:
        assert f"chunk_id: {sources[1].chunk_id}" not in user_prompt
        assert len(answer.citations[0].content) < len(sources[2].content)
    public = RagAskData.from_service_result(answer).model_dump(mode="json")
    used_graph = [json.loads(block) for block in user_prompt.split("【知识图谱辅助证据】\n")[1].split("\n\n")]
    assert [e["source_citations"] for e in public["graph"]["evidence"]] == [e["source_citations"] for e in used_graph]
    assert set(public) == {"question", "answer", "context_status", "citations", "retrieval", "llm", "graph"}
    for entry in public["retrieval"]["items"]:
        assert not any("rerank" in key for key in entry)
    assert sources[2].content == "evidence-C " * 300


@pytest.mark.parametrize("keep_normal", [True, False])
def test_actual_hybrid_deletion_filter_before_rerank(monkeypatch, keep_normal):
    sources = items(4)
    cfg = settings(graph_retrieval_enabled=True)
    events = []
    states = {UUID(sources[0].document_id): "normal" if keep_normal else "deleting",
              UUID(sources[1].document_id): "deleting", UUID(sources[2].document_id): "delete_failed"}

    def scalars(statement):
        compiled = statement.compile()
        assert "documents.deletion_status" in str(compiled) and "normal" in compiled.params.values()
        events.append("filter")
        return SimpleNamespace(all=lambda: [key for key, state in states.items() if state == "normal"])

    monkeypatch.setattr(hybrid_search, "SessionLocal", lambda: SimpleNamespace(
        scalars=scalars, close=lambda: events.append("filter-closed")))
    monkeypatch.setattr(hybrid_search, "get_embedding_provider", lambda _: FakeEmbeddingProvider())
    hits = make_response(*(make_hit(asdict(source), 5.0) for source in reversed(sources)))
    client = FakeSearchClient([hits, hits])
    monkeypatch.setattr(hybrid_search, "get_search_engine_client", lambda _: client)
    hybrid = Mock(wraps=hybrid_search.hybrid_search_chunks)
    monkeypatch.setattr(hybrid_search, "hybrid_search_chunks", hybrid)
    fuse = hybrid_search.fuse_hybrid_results

    def rrf(*args, **kwargs):
        assert events == ["filter", "filter-closed"]
        events.append("rrf")
        return fuse(*args, **kwargs)

    monkeypatch.setattr(hybrid_search, "fuse_hybrid_results", rrf)
    fake = FakeRerankingService()

    def rerank(request):
        assert events == ["filter", "filter-closed", "rrf"]
        events.append("rerank")
        return fake.rerank(request)

    getter = Mock(return_value=SimpleNamespace(rerank=rerank))
    monkeypatch.setattr(reranking, "get_reranking_service", getter)
    graph, repo = graph_service(cfg)
    llm = FakeLLMProvider()
    answer = rag.answer_question(object(), "question", limit=8, settings=cfg, llm_provider=llm, graph_retrieval=graph)
    hybrid.assert_called_once()
    assert len(client.search_calls) == 2
    if keep_normal:
        assert [c.chunk_id for c in fake.calls[0].candidates] == [sources[0].chunk_id]
        assert fake.calls[0].candidates[0].original_rank == 1
        assert len(answer.citations) == 1
    else:
        getter.assert_not_called()
        assert answer.context_status == "no_context" and not llm.calls
        repo.fetch_table_context.assert_not_called()


def test_disabled_matches_legacy_context_prompt_citation_graph(monkeypatch):
    from app.rag.citations import build_citations
    cfg = settings(reranker_enabled=False, graph_retrieval_enabled=True)
    h = harness(monkeypatch, config=cfg)
    graph, repo = graph_service(cfg)
    answer = ask(h, graph=graph)
    context = rag.build_rag_context("question", h.snapshots[0], cfg)
    assert answer.citations == build_citations(context)
    expected = rag.build_prompt("question", context, cfg, graph_context=answer.graph_context)
    assert prompts(h.llm) == (expected.system_prompt, expected.user_prompt)
    assert [p.chunk_id for p in repo.fetch_table_context.call_args.args[0].provenance] == [c.chunk_id for c in context.chunks]
    assert h.orders[0] is False
    h.getter.assert_not_called()


def test_reranker_exception_keeps_http_200_and_schema(monkeypatch):
    from app.api.v1.rag import get_db
    from app.main import create_app
    h = harness(monkeypatch, failure=RuntimeError("private path"))
    monkeypatch.setattr(rag, "get_llm_provider", lambda: h.llm)
    app = create_app(settings=h.config)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        response = client.post("/api/v1/rag/ask", json={"question": "question", "limit": 2})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["retrieval"]["limit"] == data["retrieval"]["total"] == 2
    assert set(data["retrieval"]["items"][0]) == set(asdict(h.sources[0]))
    assert "private" not in response.text
    h.hybrid.assert_called_once()


@pytest.mark.parametrize("stage", ["hybrid", "llm"])
def test_existing_hybrid_and_llm_errors_are_not_fail_open(monkeypatch, stage):
    h = harness(monkeypatch)
    error = BusinessError(LLM_TIMEOUT if stage == "llm" else HYBRID_SEARCH_CONFIG_INVALID, "original", status_code=400)
    if stage == "hybrid":
        h.hybrid.side_effect = error
    else:
        h.llm.exc = error
    with pytest.raises(BusinessError) as caught:
        ask(h)
    assert caught.value is error
    h.hybrid.assert_called_once()


@pytest.mark.parametrize("enabled", [False, True])
def test_repeated_app_lifecycle_closes_clears_and_stays_lazy(monkeypatch, enabled):
    from app.main import create_app
    services = []
    cfg = settings(reranker_enabled=enabled)
    for _ in range(2):
        app = create_app(settings=cfg)
        with TestClient(app) as client:
            assert reranking._service_cache is None
            assert client.get("/health").status_code == 200
            svc = reranking.get_reranking_service(cfg)
            assert svc._thread is None and svc._provider is None
            services.append(svc)
        assert svc.wait_closed(0)
        assert reranking._service_cache is None
    assert services[0] is not services[1]
    reranking.close_reranking_service()


def test_cache_clear_waits_for_old_worker_and_get_during_close_cannot_create_second(monkeypatch):
    from test_reranker_provider import FakeProvider
    from test_reranker_runtime import request
    fake = FakeProvider()
    fake.block = True
    cfg = settings(reranker_timeout_seconds=0.03)
    svc = reranking.RerankingService(cfg, provider_factory=lambda _: fake)
    monkeypatch.setattr(reranking, "_service_cache", svc)
    assert svc.rerank(request()).failure_reason == "timeout"
    started = Event()
    close = svc.close

    def track_close():
        started.set()
        close()

    monkeypatch.setattr(svc, "close", track_close)
    closer = Thread(target=reranking.close_reranking_service)
    closer.start()
    try:
        assert started.wait(1)
        assert reranking.get_reranking_service(cfg) is svc
        assert fake.closed == 0
    finally:
        fake.release.set()
        closer.join(2)
    assert not closer.is_alive() and fake.closed == 1
    assert reranking._service_cache is None
    fresh = reranking.get_reranking_service(cfg)
    assert fresh is not svc and fresh._thread is None
    reranking.close_reranking_service()


def test_out_of_contract_candidate_count_never_exceeds_configured_capacity(monkeypatch):
    h = harness(monkeypatch, config=settings(reranker_candidate_limit=8))
    oversized = items(9)
    result = HybridSearchResult("question", 8, 9, oversized)
    selected = rag.optional_rerank_chunks("question", result, h.config, limit=2)
    assert selected.search_result.items == oversized[:2]
    assert selected.applied is False and selected.fallback_reason == "invalid_output"
    h.getter.assert_not_called()


def test_late_second_closer_cannot_clear_new_lifespan_singleton(monkeypatch):
    cfg = settings()
    old = reranking.get_reranking_service(cfg)
    second_entered, release_second = Event(), Event()
    close = old.close
    call_count = 0

    def delayed_close():
        nonlocal call_count
        call_count += 1
        close()
        if call_count == 1:
            second_entered.set()
            assert release_second.wait(3)

    monkeypatch.setattr(old, "close", delayed_close)
    delayed = Thread(target=reranking.close_reranking_service)
    delayed.start()
    try:
        assert second_entered.wait(1)
        reranking.close_reranking_service()
        fresh = reranking.get_reranking_service(cfg)
        assert fresh is not old
    finally:
        release_second.set()
        delayed.join(2)
    assert not delayed.is_alive()
    assert reranking.get_reranking_service(cfg) is fresh


@pytest.mark.parametrize("k", [0, -1, 51])
@pytest.mark.parametrize("enabled", [False, True])
def test_public_api_limit_errors_unchanged_with_reranker(monkeypatch, k, enabled):
    from app.api.v1.rag import get_db
    from app.main import create_app
    h = harness(monkeypatch, config=settings(reranker_enabled=enabled))
    app = create_app(settings=h.config)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        response = client.post("/api/v1/rag/ask", json={"question": "question", "limit": k})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == (HYBRID_SEARCH_CONFIG_INVALID if k == 51 else RAG_CONFIG_INVALID)
    assert h.hybrid.call_count == int(k == 51)
    h.getter.assert_not_called()


def test_service_defaults_use_public_rag_top_k_and_ignore_deprecated_reranker_top_k(monkeypatch):
    h = harness(monkeypatch, config=settings(rag_top_k=2, reranker_top_k=999))
    answer = rag.answer_question(object(), "question", settings=h.config, llm_provider=h.llm)
    assert h.hybrid.call_args.kwargs["limit"] == 32
    assert answer.retrieval.limit == answer.retrieval.total == 2


@pytest.mark.parametrize("missing", ["reranker_device", "reranker_dtype"])
def test_missing_configuration_attribute_is_baseline_before_hybrid(monkeypatch, missing):
    values = settings().model_dump()
    values.pop(missing)
    h = harness(monkeypatch, config=SimpleNamespace(**values))
    assert ask(h).retrieval.items == h.sources[:2]
    assert h.hybrid.call_args.kwargs["limit"] == 2
    h.getter.assert_not_called()


def test_m1_worker_timeout_then_busy_both_continue_rag_without_retry(monkeypatch):
    from test_reranker_provider import FakeProvider
    cfg = settings(reranker_timeout_seconds=0.03)
    h = harness(monkeypatch, config=cfg)
    provider = FakeProvider()
    provider.block = True
    svc = reranking.RerankingService(cfg, provider_factory=lambda _: provider)
    h.getter.return_value = svc
    try:
        timeout_answer = ask(h)
        assert provider.entered.is_set() and provider.calls[0][1].cancelled.is_set()
        busy_answer = ask(h)
        assert timeout_answer.retrieval.items == busy_answer.retrieval.items == h.sources[:2]
        assert h.hybrid.call_count == 2 and len(provider.calls) == 1
        assert h.orders == [False, False] and len(h.llm.calls) == 2
    finally:
        provider.release.set()
        svc.close()
