"""M6.5 public projection of the exact graph evidence used in the prompt."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from unittest.mock import Mock

import pytest

from app.graph.models import GraphDocument, KGRef
from app.schemas.rag import RagAskData
from app.services import rag
from test_graph_context import build, evidence, render
from test_rag_graph_fusion import ask, item, prompts, settings


@pytest.fixture(autouse=True)
def no_neo4j_network(monkeypatch):
    from neo4j import GraphDatabase
    monkeypatch.setattr(GraphDatabase, "driver", lambda *a, **k: pytest.fail("fake graph only"))


def public(answer):
    return RagAskData.from_service_result(answer).model_dump(mode="json")


def project(context, triggered=True):
    from app.rag.graph_response import build_graph_response
    result = build_graph_response(context, triggered=triggered)
    return result.model_dump(mode="json") if result is not None else None


def test_disabled_is_null_and_never_queries(monkeypatch):
    answer, llm, repo = ask(monkeypatch, [item()], config=settings(graph_retrieval_enabled=False))
    assert public(answer)["graph"] is None
    repo.fetch_table_context.assert_not_called()
    assert len(llm.calls) == 1


@pytest.mark.parametrize("metadata", [{}, {"parser_provider": "mineru"}, {"kg_refs": []}, {"kg_refs": [None]}])
def test_enabled_without_refs_is_not_triggered(monkeypatch, metadata):
    answer, _, repo = ask(monkeypatch, [item(metadata=metadata)])
    assert public(answer)["graph"] == {
        "enabled": True, "triggered": False, "status": "not_triggered",
        "truncated": False, "evidence_count": 0, "evidence": [],
    }
    repo.fetch_table_context.assert_not_called()


def test_no_context_remains_safe_without_llm_or_graph(monkeypatch):
    answer, llm, repo = ask(monkeypatch, [])
    assert public(answer)["graph"]["status"] == "not_triggered"
    assert answer.context_status == "no_context" and not llm.calls
    repo.fetch_table_context.assert_not_called()


def test_explicit_safe_dtos_and_relationship_names_without_mutation():
    raw = evidence(citations=(2, 1), document=GraphDocument("D", "C:/private/secret.pdf", "pdf"))
    context = build(raw)
    before = deepcopy(asdict(context))
    data = project(context)
    assert data["status"] == "success" and data["evidence_count"] == 1
    node = data["evidence"][0]
    assert set(node) == {"graph_id", "anchor_id", "anchor_type", "table_ref", "source_citations",
                         "document", "table", "entities", "relationships"}
    assert node["source_citations"] == [1, 2]
    assert node["document"] == {"doc_id": "D"}
    assert node["table"] == {"table_id": "D_T-P8-1", "table_ref": "T-P8-1", "page": 8, "table_index": 1}
    assert node["entities"] == [{"id": e.id, "name": e.name, "entity_type": e.entity_type, "page": e.page}
                                for e in raw.entities]
    assert node["relationships"] == [{"source_entity_id": "E1", "source_name": raw.entities[0].name,
        "type": "规定", "target_entity_id": "E2", "target_name": "材料"}]
    serialized = json.dumps(data)
    for forbidden in ("secret.pdf", "elementId", "diagnostics", "template_name", "source_range", "password"):
        assert forbidden not in serialized
    assert asdict(context) == before
    data["evidence"][0]["entities"][0]["name"] = "changed"
    assert asdict(context) == before


def test_multiple_evidence_order_and_nullable_page_are_preserved():
    raw = evidence("B", "G2")
    raw = replace(raw, entities=(replace(raw.entities[0], page=None), *raw.entities[1:]))
    context = build(raw, evidence("A", "G1"))
    data = project(context)
    assert data["evidence_count"] == 2
    assert [e["anchor_id"] for e in data["evidence"]] == ["A", "B"]
    assert data["evidence"][1]["entities"][0]["page"] is None


def test_final_budgeted_records_match_prompt_not_raw_retrieval():
    raw = evidence()
    full = build(raw)
    context = build(raw, max_chars=full.total_chars - 1)
    assert context.was_truncated
    data = project(context)
    prompt_data = json.loads(render(context))
    assert data["truncated"] is True and data["status"] == "success"
    node = data["evidence"][0]
    assert [e["id"] for e in node["entities"]] == [e["id"] for e in prompt_data["entities"]]
    assert len(node["relationships"]) == len(prompt_data["relationships"]) < len(raw.relationships)


@pytest.mark.parametrize("status", ["unavailable", "timeout", "budget_exhausted", "not_found", "ambiguous",
                                   "unsupported_anchor_type", "conflicting_ref", "invalid_ref"])
def test_failure_statuses_are_coarse_and_safe(monkeypatch, status):
    answer, llm, _ = ask(monkeypatch, [item()], statuses={"A": status})
    data = public(answer)["graph"]
    assert data == {"enabled": True, "triggered": True, "status": "unavailable",
                    "truncated": False, "evidence_count": 0, "evidence": []}
    assert len(llm.calls) == 1 and "知识图谱辅助证据" not in prompts(llm)[1]


def test_partial_keeps_only_successful_evidence(monkeypatch):
    refs = [asdict(KGRef(n, "G", "table", "T-P8-1")) for n in ("A", "B")]
    answer, llm, repo = ask(monkeypatch, [item(refs=refs)], statuses={"B": "timeout"})
    data = public(answer)["graph"]
    assert data["status"] == "partial" and data["evidence_count"] == 1
    assert [e["anchor_id"] for e in data["evidence"]] == ["A"]
    assert '"anchor_id":"A"' in prompts(llm)[1] and '"anchor_id":"B"' not in prompts(llm)[1]
    repo.fetch_table_context.assert_called_once()


def test_same_final_context_shared_and_mapping_never_queries(monkeypatch):
    captured = []
    original = rag.build_graph_context
    def capture(*args, **kwargs):
        context = original(*args, **kwargs)
        captured.append(context)
        return context
    monkeypatch.setattr(rag, "build_graph_context", capture)
    answer, llm, repo = ask(monkeypatch, [item(), item(2)])
    assert answer.graph_context is captured[0]
    before = deepcopy(asdict(answer.graph_context))
    first = public(answer)
    assert first == public(answer)
    assert asdict(answer.graph_context) == before
    repo.fetch_table_context.assert_called_once()
    assert len(llm.calls) == 1
    graph = first["graph"]["evidence"][0]
    used = json.loads(prompts(llm)[1].split("【知识图谱辅助证据】\n", 1)[1])
    assert graph["source_citations"] == used["source_citations"] == [1, 2]
    assert [e["id"] for e in graph["entities"]] == [e["id"] for e in used["entities"]]


@pytest.mark.parametrize("failure", ["formatter", "empty_formatter", "builder", "retriever"])
def test_fallback_never_exposes_unused_evidence(monkeypatch, failure):
    import app.rag.prompt as prompt_module
    graph = None
    if failure == "formatter":
        monkeypatch.setattr(prompt_module, "format_graph_context_for_prompt", Mock(side_effect=ValueError("private")))
    elif failure == "empty_formatter":
        monkeypatch.setattr(prompt_module, "format_graph_context_for_prompt", lambda _: "")
    elif failure == "builder":
        monkeypatch.setattr(rag, "build_graph_context", Mock(side_effect=ValueError("private")))
    else:
        graph = Mock()
        graph.retrieve.side_effect = RuntimeError("neo4j://user:password@private")
    # A literal heading in text must not be mistaken for an applied graph section.
    answer, llm, _ = ask(monkeypatch, [item(content="正文提到【知识图谱辅助证据】这个标题。")], graph=graph)
    data = public(answer)["graph"]
    assert data["triggered"] and data["status"] == "unavailable" and data["evidence"] == []
    assert '"anchor_id":"A"' not in prompts(llm)[1]
    assert "private" not in json.dumps(data)


def test_empty_budget_has_no_evidence_and_exposes_truncation(monkeypatch):
    answer, _, _ = ask(monkeypatch, [item()], config=settings(rag_graph_context_max_chars=1))
    data = public(answer)["graph"]
    assert data["evidence"] == [] and data["truncated"] is True


def test_existing_fields_are_identical_with_graph_enabled_or_disabled(monkeypatch):
    sources = [item()]
    off, _, _ = ask(monkeypatch, sources, config=settings(graph_retrieval_enabled=False))
    on, _, _ = ask(monkeypatch, sources)
    old = public(off)
    new = public(on)
    old.pop("graph")
    new.pop("graph")
    assert old == new
    assert set(old) == {"question", "answer", "context_status", "citations", "retrieval", "llm"}


def test_graph_is_optional_for_old_service_results_and_clients():
    from test_rag_api import make_client, make_service_result
    legacy = RagAskData.from_service_result(make_service_result())
    payload = legacy.model_dump(mode="json")
    assert payload.pop("graph") is None
    assert RagAskData.model_validate(payload).graph is None
    schema = make_client().app.openapi()["components"]["schemas"]["RagAskData"]
    assert "graph" in schema["properties"] and "graph" not in schema["required"]


def test_api_returns_graph_used_by_same_single_llm_call(monkeypatch):
    from fastapi.testclient import TestClient
    from app.api.v1.rag import get_db
    import app.main as main
    from test_rag_graph_fusion import graph_service, result
    from test_rag_service import FakeLLMProvider
    config = settings()
    _, repo = graph_service(config)
    monkeypatch.setattr(main, "Neo4jRepository", lambda _: repo)
    monkeypatch.setattr(rag, "retrieve_chunks", lambda *a, **k: result([item()]))
    llm = FakeLLMProvider()
    monkeypatch.setattr(rag, "get_llm_provider", lambda: llm)
    app = main.create_app(settings=config)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        response = client.post("/api/v1/rag/ask", json={"question": "question"})
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["graph"]["evidence_count"] == 1
        assert data["graph"]["evidence"][0]["source_citations"] == [1]
        assert len(llm.calls) == 1
        repo.fetch_table_context.assert_called_once()
        assert not any("graph" in path for path in app.openapi()["paths"])
