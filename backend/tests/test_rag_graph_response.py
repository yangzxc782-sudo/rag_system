from dataclasses import replace
import pytest
from app.graph.models import GraphRetrievalResult
from app.rag.graph_context_builder import build_graph_context
from app.rag.graph_response import build_graph_response
from app.schemas.rag import RagAskData
from graph_v2_support import binding, item
from test_graph_context import evidence
from test_rag_graph_fusion import ask


@pytest.mark.parametrize("kind", ["table","clause"])
def test_public_v2_actual_source_schema_and_no_private_path(kind):
    a=binding(kind=kind)
    graph=build_graph_context(GraphRetrievalResult((evidence(a),)))
    public=build_graph_response(graph,triggered=True)
    assert public.schema_version==2 and public.status=="success"
    data=public.model_dump(mode="json")
    assert data["evidence"][0]["anchor"]["anchor_type"]==kind
    assert data["evidence"][0]["source"]["document_id"]==a.source.document_id
    assert "source_path" not in str(data) and "canonical_sha256" not in str(data)
    assert not {"table","document"} & data["evidence"][0].keys()
    if kind=="clause": assert "table_ref" not in data["evidence"][0]["anchor"]


def test_query_success_without_usage_reported_separately():
    a=binding()
    a=replace(a,provenance=(replace(a.provenance[0],effective_end=10),))
    public=build_graph_response(build_graph_context(GraphRetrievalResult((evidence(a),))),triggered=True)
    assert public.status=="not_used" and public.evidence_count==0
    assert public.diagnostics[0].mapped and public.diagnostics[0].query_status=="success"
    assert not public.diagnostics[0].facts_used


def test_public_ask_projection_keeps_citations(monkeypatch):
    answer,_,_=ask(monkeypatch,[item(kind="clause")])
    public=RagAskData.from_service_result(answer)
    assert public.graph.evidence_count==1 and public.citations[0].content==item().content
    assert public.graph.evidence[0].source_citations==[1]
