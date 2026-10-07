"""Synthetic single-turn tests; production SQL source admission has a separate suite."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from app.graph.models import GraphRetrievalRequest, GraphAnchorResult
from app.rag.context_builder import build_rag_context, format_context_for_prompt, TRUNCATION_MARKER
from app.rag.prompt import build_system_prompt, build_user_prompt
from app.services import rag
from app.services.graph_retrieval import GraphRetrievalService
from graph_v2_support import settings, item, FixtureAuthority, repository, FakeDriver, row
from test_rag_service import FakeLLMProvider


def result(items):
    return SimpleNamespace(query="question", limit=8, total=len(items), items=items)


def graph_service(config, statuses=None):
    repo = Mock()
    decoder, _, _ = repository()
    from app.extraction.kg_extract import template
    def fetch(request):
        return tuple(replace(decoder._decode(a, row(a), template()),
            status=(statuses or {}).get(a.ref.anchor_id, "success")) for a in request.anchors)
    repo.fetch_anchor_context.side_effect = fetch
    return GraphRetrievalService(config, repository=repo, authority=FixtureAuthority()), repo


def ask(monkeypatch, items, *, config=None, statuses=None, graph=None):
    config = config or settings()
    service, repo = graph_service(config, statuses)
    monkeypatch.setattr(rag, "retrieve_chunks", lambda *a, **k: result(items))
    llm = FakeLLMProvider("回答。[1]")
    answer = rag.answer_question(object(), "question", settings=config, llm_provider=llm,
        graph_retrieval=graph if graph is not None else service)
    return answer, llm, repo


def prompts(llm):
    return tuple(message.content[0].text for message in llm.calls[0].messages)


@pytest.mark.parametrize("kind", ["table","clause"])
def test_new_anchor_single_turn(kind, monkeypatch):
    answer, llm, repo = ask(monkeypatch, [item(kind=kind)])
    repo.fetch_anchor_context.assert_called_once()
    assert answer.graph_triggered and answer.graph_context.evidence[0].ref.anchor_type == kind
    assert "知识图谱辅助证据" in prompts(llm)[1] and "350" in prompts(llm)[1]
    assert answer.citations[0].content == item().content


def test_feature_off_exact_text_baseline(monkeypatch):
    config = settings(graph_retrieval_enabled=False)
    source = item()
    answer,llm,repo = ask(monkeypatch,[source],config=config)
    context = build_rag_context("question",result([source]),config)
    assert prompts(llm) == (build_system_prompt(config),build_user_prompt("question",format_context_for_prompt(context)))
    repo.fetch_anchor_context.assert_not_called()


def test_partial_prefix_still_queries_but_does_not_inject_facts(monkeypatch):
    source = item(content="完整条款 "*100)
    full = build_rag_context("question",result([source]),settings())
    overhead = full.total_chars-len(source.content)
    answer,llm,repo = ask(monkeypatch,[source],config=settings(rag_context_max_chars=overhead+30))
    repo.fetch_anchor_context.assert_called_once()
    assert answer.citations[0].content.endswith(TRUNCATION_MARKER)
    assert not answer.graph_context.evidence and answer.graph_context.diagnostics[0].use_status == "incomplete_coverage"
    assert "知识图谱辅助证据" not in prompts(llm)[1]


def test_no_graph_fallback_for_legacy_mode_or_missing_authority(monkeypatch):
    answer,_,repo = ask(monkeypatch,[item()],config=settings(pdf_kg_search_enabled=False))
    assert answer.graph_context.source_error == "v2_admission_disabled"
    repo.fetch_anchor_context.assert_not_called()
    source=item(); source.chunk_set_id=None
    answer,_,repo=ask(monkeypatch,[source])
    assert answer.graph_context.source_error == "source_invalid"
    repo.fetch_anchor_context.assert_not_called()


def test_neo4j_failure_keeps_text_answer(monkeypatch):
    answer,llm,_=ask(monkeypatch,[item()],statuses={"G1::T-1":"timeout"})
    assert answer.answer and answer.citations and not answer.graph_context.evidence
    assert answer.graph_context.diagnostics[0].query_status == "timeout"
    assert "知识图谱辅助证据" not in prompts(llm)[1]


def test_admission_changed_after_query_drops_facts(monkeypatch):
    config=settings()
    graph,repo=graph_service(config)
    original=graph.authority.resolve
    graph.authority.resolve=Mock(side_effect=[original(build_rag_context("q",result([item()]),config)), ()])
    answer,_,_=ask(monkeypatch,[item()],config=config,graph=graph)
    assert not answer.graph_context.evidence and answer.graph_context.source_error == "source_changed"


def test_formatter_failure_marks_not_used(monkeypatch):
    import app.rag.prompt as prompt
    monkeypatch.setattr(prompt,"format_graph_context_for_prompt",Mock(side_effect=ValueError("bad")))
    answer,llm,_=ask(monkeypatch,[item()])
    assert not answer.graph_context.evidence
    assert answer.graph_context.diagnostics[0].use_status == "prompt_omitted"
    assert "知识图谱辅助证据" not in prompts(llm)[1]
