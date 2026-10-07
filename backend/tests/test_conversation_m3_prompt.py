from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.rag.conversation_prompt import checked_request, input_budget, prompt_cost, serialized_messages, prompt_fingerprint
from app.rag.graph_context_builder import GraphContext, format_graph_context_for_prompt
from app.rag.context_builder import RagContext
from app.rag.history_budget import HistoryContext, HistoryMessage
from app.services.chat_evidence import ChatEvidenceService
from app.services.hybrid_search import HybridSearchResult
from phase13_m2_support import settings_for
from test_rag_reranking import items
from test_rag_graph_fusion import graph_service
from graph_v2_support import with_content, FixtureAuthority, nested_properties, reorder_objects


def build_settings(**changes):
    return settings_for(conversation_graph_version="phase13_m3_v2", pdf_kg_search_enabled=True, **changes)


def test_serialized_total_budget_and_current_citations_are_consistent():
    settings = build_settings(conversation_answer_max_input_tokens=4096, graph_retrieval_enabled=False)
    tid = uuid4()
    history = HistoryContext((HistoryMessage(uuid4(), tid, "user", "冒口是什么？", 1),
        HistoryMessage(uuid4(), tid, "assistant", "历史回答引用 [98]，不是当前事实。", 2)))
    candidates = [with_content(s, "铸件补缩依据" * 400) for s in items(4)]
    plan = ChatEvidenceService(None, settings).build("那它尺寸呢？", "冒口尺寸呢？", history,
        HybridSearchResult("冒口尺寸呢？", 4, 4, candidates), preserve_order=True)
    request = checked_request("那它尺寸呢？", "冒口尺寸呢？", plan.history, plan.context, plan.graph, settings)
    assert prompt_cost(request.messages) <= input_budget(settings)
    assert plan.context.chunks and plan.context.chunks[0].was_truncated
    assert "[98]" not in str(serialized_messages(request.messages))
    assert "历史引用98" in request.messages[2].content[0].text
    assert "那它尺寸呢？" in request.messages[-1].content[0].text
    assert [c.citation_id for c in plan.context.chunks] == list(range(1, len(plan.context.chunks) + 1))


def test_graph_only_uses_final_budgeted_text_and_can_fail_open():
    settings = build_settings(graph_retrieval_enabled=True, conversation_answer_max_input_tokens=5000)
    sources = [with_content(s, "文本证据" * 1000) for s in items(4)]
    service = Mock()
    service.authority = FixtureAuthority()
    service.retrieve.side_effect = RuntimeError("optional graph failure")
    plan = ChatEvidenceService(None, settings, graph_retrieval=service).build("冒口是什么？", "冒口是什么？",
        HistoryContext(), HybridSearchResult("冒口是什么？", 4, 4, sources), preserve_order=True)
    assert plan.context.context_status == "ok" and not plan.graph.evidence
    assert service.retrieve.call_count == 1
    used = {p.chunk_id for a in service.retrieve.call_args.args[0] for p in a.provenance}
    assert used == {c.chunk_id for c in plan.context.chunks}
    assert len(used) < 4


def test_graph_success_budget_preserves_citation_mapping():
    settings = build_settings(graph_retrieval_enabled=True, conversation_answer_graph_tokens=4096)
    graph, _ = graph_service(settings)
    sources = items(2)
    plan = ChatEvidenceService(None, settings, graph_retrieval=graph).build("冒口是什么？", "冒口是什么？",
        HistoryContext(), HybridSearchResult("冒口是什么？", 2, 2, sources), preserve_order=True)
    assert plan.graph.evidence
    assert len(format_graph_context_for_prompt(plan.graph).encode()) <= settings.conversation_answer_graph_tokens
    ids = {c.citation_id for c in plan.context.chunks}
    assert all(set(e.source_citations) <= ids for e in plan.graph.evidence)


def test_known_window_reserves_output_and_safety():
    settings = build_settings(conversation_model_context_window=8192, llm_max_tokens=2048)
    assert input_budget(settings) == 8192 - 2048 - 1024


def test_fixed_question_over_budget_is_an_error_not_an_answer():
    settings = build_settings(conversation_answer_max_input_tokens=1024)
    with pytest.raises(Exception, match="budget"):
        ChatEvidenceService(None, settings).build("问题" * 1000, "问题" * 1000, HistoryContext(),
            HybridSearchResult("q", 8, 0, []), preserve_order=False)


@pytest.mark.parametrize("reserve", [3000, 20000])
def test_property_order_does_not_change_phase13_budget_selection(reserve):
    settings = build_settings(graph_retrieval_enabled=True, conversation_answer_graph_tokens=reserve,
        conversation_answer_max_input_tokens=100000, rag_graph_context_max_chars=20000)
    sources = items(2)
    plans = []
    for props in (nested_properties(), reorder_objects(nested_properties())):
        service, repo = graph_service(settings)
        fetch = repo.fetch_anchor_context.side_effect
        def with_properties(request):
            return tuple(replace(item,
                entities=tuple(replace(e, properties=props) for e in item.entities),
                relationships=tuple(replace(r, properties=props) for r in item.relationships))
                for item in fetch(request))
        repo.fetch_anchor_context.side_effect = with_properties
        plans.append(ChatEvidenceService(None, settings, graph_retrieval=service).build("q", "q",
            HistoryContext(), HybridSearchResult("q", 2, 2, sources), preserve_order=True))
    a, b = plans
    assert a.context == b.context and a.graph.diagnostics == b.graph.diagnostics
    assert [g.ref for g in a.graph.evidence] == [g.ref for g in b.graph.evidence]
    assert format_graph_context_for_prompt(a.graph) == format_graph_context_for_prompt(b.graph)
    assert a.graph.total_chars == b.graph.total_chars
    requests = [checked_request("q", "q", p.history, p.context, p.graph, settings) for p in plans]
    assert requests[0].messages == requests[1].messages
    assert prompt_cost(requests[0].messages) == prompt_cost(requests[1].messages)
    if reserve == 20000:
        assert len(a.graph.evidence) == 2


@pytest.mark.parametrize("enabled,expected", [
    (False, "b9c1eec03d8150eb47dc6539e5b9696da61b291c16f2b71b63b2226d58423cda"),
    (True, "81cc0f1b53ff1c6ad0c841f7042b42ed7388d9bca4ffc52f050a964ee7799d41"),
])
def test_empty_graph_keeps_pre_fix_prompt_fingerprint(enabled, expected):
    # Synthetic q/empty-context golden values captured before the formatter fix.
    graph = GraphContext(enabled=enabled, status="empty" if enabled else "disabled")
    request = checked_request("q", "q", HistoryContext(), RagContext("q", "no_context", [], 0, 0),
        graph, build_settings(graph_retrieval_enabled=enabled))
    assert format_graph_context_for_prompt(graph) == ""
    assert prompt_fingerprint(request.messages) == expected
