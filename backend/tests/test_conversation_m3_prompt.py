from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.rag.conversation_prompt import checked_request, input_budget, prompt_cost, serialized_messages
from app.rag.graph_context_builder import format_graph_context_for_prompt
from app.rag.history_budget import HistoryContext, HistoryMessage
from app.services.chat_evidence import ChatEvidenceService
from app.services.hybrid_search import HybridSearchResult
from phase13_m2_support import settings_for
from test_rag_reranking import items
from test_rag_graph_fusion import graph_service


def build_settings(**changes):
    return settings_for(conversation_graph_version="phase13_m3_v2", **changes)


def test_serialized_total_budget_and_current_citations_are_consistent():
    settings = build_settings(conversation_answer_max_input_tokens=4096, graph_retrieval_enabled=False)
    tid = uuid4()
    history = HistoryContext((HistoryMessage(uuid4(), tid, "user", "冒口是什么？", 1),
        HistoryMessage(uuid4(), tid, "assistant", "历史回答引用 [98]，不是当前事实。", 2)))
    candidates = [replace(s, content="铸件补缩依据" * 400) for s in items(4)]
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
    sources = [replace(s, content="文本证据" * 1000) for s in items(4)]
    service = Mock()
    service.retrieve.side_effect = RuntimeError("optional graph failure")
    plan = ChatEvidenceService(None, settings, graph_retrieval=service).build("冒口是什么？", "冒口是什么？",
        HistoryContext(), HybridSearchResult("冒口是什么？", 4, 4, sources), preserve_order=True)
    assert plan.context.context_status == "ok" and not plan.graph.evidence
    assert service.retrieve.call_count == 1
    used = {p.chunk_id for p in service.retrieve.call_args.kwargs["provenance"]}
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
