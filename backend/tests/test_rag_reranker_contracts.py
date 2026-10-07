"""Production RAG/reranker contracts using synthetic data and fake model/storage."""
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.core.errors import BusinessError, HYBRID_SEARCH_FAILED, LLM_TIMEOUT
from app.retrieval import local_cross_encoder as runtime
from app.retrieval.reranker import RerankScore
from app.schemas.rag import RagAskData
from app.services import hybrid_search, rag, reranking
from tests.reranker_observation import RequestTrace
from test_hybrid_search import FakeEmbeddingProvider, FakeSearchClient, make_hit, make_response
from test_rag_graph_fusion import graph_service
from graph_v2_support import with_content
from test_rag_reranking import ask, harness, items, settings
from test_rag_service import FakeLLMProvider
from test_reranker_runtime import FakeTorch, FakeTokenizer, FakeModel


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    loader = Mock(side_effect=AssertionError('Contract tests must never load real BGE'))
    monkeypatch.setattr(runtime, '_load_local_model', loader)
    yield
    loader.assert_not_called()


def config(**changes):
    values = dict(reranker_dtype='bf16', reranker_device='cuda',
                  reranker_max_length=1024, reranker_candidate_limit=32,
                  reranker_batch_size=8, reranker_timeout_seconds=5.0)
    values.update(changes)
    return settings(**values)


@pytest.mark.parametrize('k,requested,scored', [(8, 32, True), (32, 32, True), (50, 50, False)])
def test_k_capacity_once_hybrid(monkeypatch, k, requested, scored):
    h = harness(monkeypatch, items(50), config=config())
    before = deepcopy([asdict(s) for s in h.sources])
    with RequestTrace(h.service) as trace:
        answer = ask(h, k=k)
    trace.verify(answer)
    assert trace.hybrid_limits == [requested]
    assert trace.reranker_ids == ([s.chunk_id for s in h.sources[:32]] if scored else [])
    expected = list(reversed(h.sources[:32]))[:k] if scored else h.sources[:k]
    assert answer.retrieval.items == expected
    assert answer.retrieval.limit == k
    assert len(h.service.calls) == int(scored)
    assert trace.fallback_reason == (None if scored else 'public_limit_exceeds_reranker_capacity')
    if not scored:
        h.getter.assert_not_called()  # No runtime, hence zero possible BGE forwards.
    assert [asdict(s) for s in h.sources] == before
    public = RagAskData.from_service_result(answer).model_dump(mode='json')
    assert set(public) == {'question', 'answer', 'context_status', 'citations', 'retrieval', 'llm', 'graph'}
    assert set(public['retrieval']['items'][0]) == set(asdict(h.sources[0]))
    assert all('rerank' not in key and 'candidate_limit' not in key for key in public['retrieval'])


@pytest.mark.parametrize('count', [0, 1, 21])
def test_short_candidates_never_refill(monkeypatch, count):
    h = harness(monkeypatch, items(count), config=config(graph_retrieval_enabled=True))
    graph, repo = graph_service(h.config)
    with RequestTrace(h.service, graph) as trace:
        answer = ask(h, k=8, graph=graph)
    trace.verify(answer)
    assert trace.hybrid_limits == [32]
    assert len(trace.reranker_ids) == count
    if not count:
        assert answer.context_status == 'no_context'
        assert answer.answer == h.config.rag_no_context_message
        assert not h.llm.calls and not h.service.calls
        h.getter.assert_not_called()
        repo.fetch_anchor_context.assert_not_called()


@pytest.mark.parametrize('failure', [RuntimeError('fault'), 'timeout', 'busy', 'invalid_output'])
def test_fail_open_keeps_original_c_snapshot_first_k(monkeypatch, failure):
    h = harness(monkeypatch, items(32), config=config(), failure=failure)
    with RequestTrace(h.service) as trace:
        answer = ask(h, k=8)
    trace.verify(answer)
    assert trace.hybrid_limits == [32] and len(h.service.calls) == 1
    assert answer.retrieval.items == h.sources[:8]
    assert all(a is b for a, b in zip(answer.retrieval.items, h.sources))
    assert trace.fallback_reason is not None


def test_invalid_partial_scores_never_apply(monkeypatch):
    h = harness(monkeypatch, items(32), config=config())
    h.service.transform = lambda result: replace(result, scores=result.scores[:-1])
    with RequestTrace(h.service) as trace:
        answer = ask(h, k=8)
    trace.verify(answer)
    assert answer.retrieval.items == h.sources[:8]
    assert trace.fallback_reason == 'invalid_output'


@pytest.mark.parametrize('graph_enabled', [True, False])
@pytest.mark.parametrize('budget', [12000, 400])
def test_changed_order_citation_prompt_and_final_context_graph(monkeypatch, graph_enabled, budget):
    sources = items(4)
    if budget == 400:
        sources[2] = with_content(sources[2], 'C evidence ' * 400)
    h = harness(monkeypatch, sources, config=config(
        graph_retrieval_enabled=graph_enabled, pdf_kg_search_enabled=True, rag_context_max_chars=budget))
    # A,B,C,D -> C,A,D,B: neither original nor reversed original ordering.
    order = [2, 0, 3, 1]
    h.service.transform = lambda result: replace(result, scores=tuple(
        RerankScore(sources[i].chunk_id, i + 1, float(4 - n)) for n, i in enumerate(order)))
    graph, repo = graph_service(h.config)
    with RequestTrace(h.service, graph) as trace:
        answer = ask(h, k=3, graph=graph)
    trace.verify(answer)
    expected = [sources[i].chunk_id for i in ([2, 0, 3] if budget == 12000 else [2])]
    assert trace.hybrid_ids == [s.chunk_id for s in sources]
    assert trace.final_ids == [sources[i].chunk_id for i in [2, 0, 3]]
    assert trace.context_ids == trace.prompt_ids == [c.chunk_id for c in answer.citations] == expected
    assert [c.citation_id for c in answer.citations] == list(range(1, len(expected) + 1))
    assert repo.fetch_anchor_context.call_count == (len(expected) if graph_enabled else 0)
    assert len(trace.graph_calls) == int(graph_enabled)
    if graph_enabled:
        call = trace.graph_calls[0]
        assert [p['chunk_id'] for p in call['provenance']] == expected
        assert [r['anchor_id'] for r in call['refs']] == [f'G{sources[i].chunk_index}::T-{sources[i].chunk_index}' for i in ([2, 0, 3] if budget == 12000 else [2])]
        assert sources[1].chunk_id not in str(call)  # Reranker eliminated B.
        if budget == 400:
            assert sources[0].chunk_id not in str(call)  # Context budget eliminated A,D.
            assert sources[3].chunk_id not in str(call)


@pytest.mark.parametrize('keep_normal', [True, False])
def test_actual_filter_before_tokenizer_and_model(monkeypatch, tmp_path, keep_normal):
    sources = items(6)
    sources[5] = replace(sources[5], document_id='invalid-document-id')
    states = {UUID(s.document_id): state for s, state in zip(sources[:5],
        ['normal' if keep_normal else 'deleting', 'deleting', 'delete_failed', None, 'unexpected']) if state is not None}
    cfg = config(reranker_model_path=str(tmp_path), graph_retrieval_enabled=True)
    def scalars(statement):
        compiled = statement.compile()
        assert 'documents.deletion_status' in str(compiled) and 'normal' in compiled.params.values()
        return SimpleNamespace(all=lambda: [doc for doc, status in states.items() if status == 'normal'])
    monkeypatch.setattr(hybrid_search, 'SessionLocal', lambda: SimpleNamespace(scalars=scalars, close=lambda: None))
    monkeypatch.setattr(hybrid_search, 'get_embedding_provider', lambda _: FakeEmbeddingProvider())
    hits = make_response(*(make_hit(asdict(s), 5.0) for s in sources))
    client = FakeSearchClient([hits, hits])
    monkeypatch.setattr(hybrid_search, 'get_search_engine_client', lambda _: client)
    torch, tokenizer = FakeTorch(), FakeTokenizer()
    model = FakeModel(torch)
    service = reranking.RerankingService(cfg, provider_factory=lambda c: runtime.LocalCrossEncoderProvider(
        c, model_loader=lambda _: runtime.LoadedCrossEncoder(tokenizer, model, torch)))
    monkeypatch.setattr(reranking, 'get_reranking_service', lambda _: service)
    graph, repo = graph_service(cfg)
    llm = FakeLLMProvider()
    try:
        with RequestTrace(service, graph) as trace:
            answer = rag.answer_question(None, 'dedicated deletion contract', limit=8, settings=cfg,
                                         llm_provider=llm, graph_retrieval=graph)
        trace.verify(answer)
        safe = [sources[0].chunk_id] if keep_normal else []
        assert set(trace.raw_ids) == {s.chunk_id for s in sources}
        assert trace.safe_ids == trace.reranker_ids == safe
        assert trace.hybrid_limits == [32] and len(client.search_calls) == 2
        assert model.calls == int(keep_normal)
        assert [p for _, passages, _ in tokenizer.calls for p in passages] == ([sources[0].content] if keep_normal else [])
        assert trace.events.index('deletion_filter') < trace.events.index('hybrid_return')
        if keep_normal:
            assert trace.events.index('hybrid_return') < trace.events.index('reranker_input')
        else:
            assert answer.context_status == 'no_context' and not llm.calls
            repo.fetch_anchor_context.assert_not_called()
    finally:
        service.close()


@pytest.mark.parametrize('stage', ['hybrid', 'llm', 'graph'])
def test_existing_error_semantics(monkeypatch, stage):
    h = harness(monkeypatch, config=config(graph_retrieval_enabled=True, pdf_kg_search_enabled=True))
    graph, repo = graph_service(h.config)
    error = BusinessError(LLM_TIMEOUT if stage == 'llm' else HYBRID_SEARCH_FAILED, 'original', status_code=503)
    if stage == 'hybrid':
        h.hybrid.side_effect = error
    elif stage == 'llm':
        h.llm.exc = error
    else:
        graph.retrieve = Mock(side_effect=RuntimeError('optional graph failure'))
    if stage != 'graph':
        with pytest.raises(BusinessError) as caught:
            ask(h, k=8, graph=graph)
        assert caught.value is error
    else:
        assert ask(h, k=8, graph=graph).context_status == 'ok'
        assert len(h.llm.calls) == 1 and graph.retrieve.call_count == 1
    h.hybrid.assert_called_once()
    if stage == 'hybrid':
        h.getter.assert_not_called()


def test_disabled_is_original_public_k(monkeypatch):
    h = harness(monkeypatch, items(32), config=config(reranker_enabled=False))
    with RequestTrace(h.service) as trace:
        answer = ask(h, k=8)
    trace.verify(answer)
    assert trace.hybrid_limits == [8] and answer.retrieval.items == h.sources[:8]
    h.getter.assert_not_called()


def test_trace_detects_prompt_order_regression(monkeypatch):
    h = harness(monkeypatch, config=config())
    with RequestTrace(h.service) as trace:
        answer = ask(h, k=3)
    trace.prompt_ids.reverse()
    with pytest.raises(AssertionError, match='prompt/context'):
        trace.verify(answer)
