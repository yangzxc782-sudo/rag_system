import pytest

from app.services.chat_evidence import ChatEvidenceService
from test_rag_reranking import harness, items, settings


def allow_explicit_filter_factory(h):
    original = h.hybrid.side_effect
    h.hybrid.side_effect = lambda *args, deletion_filter_session_factory, **kwargs: original(*args, **kwargs)


@pytest.mark.parametrize("enabled,k,expected,calls", [(False, 8, 8, 0), (True, 8, 32, 1), (True, 32, 32, 1), (True, 50, 50, 0)])
def test_chat_uses_shared_phase12_k_c_contract(monkeypatch, enabled, k, expected, calls):
    h = harness(monkeypatch, items(50), config=settings(reranker_enabled=enabled, reranker_candidate_limit=32))
    allow_explicit_filter_factory(h)
    factory = object()
    stage = ChatEvidenceService(factory, h.config).retrieve("独立问题", k, None)
    assert h.hybrid.call_count == 1
    assert h.hybrid.call_args.kwargs["limit"] == expected
    assert h.hybrid.call_args.kwargs["deletion_filter_session_factory"] is factory
    assert len(h.service.calls) == calls
    expected_items = list(reversed(h.sources[:32]))[:k] if calls else h.sources[:k]
    assert stage.outcome.search_result.items == expected_items
    assert [i.hybrid_score for i in stage.outcome.search_result.items] == [i.hybrid_score for i in expected_items]


@pytest.mark.parametrize("failure", ["busy", "timeout", RuntimeError("synthetic failure")])
def test_chat_fail_open_does_not_repeat_hybrid(monkeypatch, failure):
    h = harness(monkeypatch, items(32), config=settings(reranker_candidate_limit=32), failure=failure)
    allow_explicit_filter_factory(h)
    stage = ChatEvidenceService(object(), h.config).retrieve("独立问题", 8, None)
    assert h.hybrid.call_count == 1 and len(h.service.calls) == 1
    assert stage.outcome.search_result.items == h.sources[:8]
    assert not stage.outcome.applied and stage.outcome.fallback_reason
