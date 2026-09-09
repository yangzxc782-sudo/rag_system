from __future__ import annotations

from dataclasses import asdict, replace
import json

import pytest

from app.graph.models import (
    GraphAnchorResult, GraphDocument, GraphEntity, GraphRelationship,
    GraphRetrievalResult, GraphTable, GraphTriggerProvenance, KGRef,
)


def evidence(anchor="A", graph="G", citations=(1,), **changes):
    ref = KGRef(anchor, graph, "table", "T-P8-1")
    item = GraphAnchorResult(
        ref=ref, status="success", template_name="table_context_v1",
        document=GraphDocument("D", "source.pdf", "pdf"),
        table=GraphTable("D_T-P8-1", "T-P8-1", 8, 1),
        entities=(GraphEntity("E1", "0.25 ≤0.5% 600℃", "参数", "D", "T-P8-1", 8),
                  GraphEntity("E2", "材料", "材料", "D", "T-P8-1", 8)),
        relationships=(GraphRelationship("规定", "E1", "E2", graph, "D"),),
        provenance=tuple(GraphTriggerProvenance(anchor, f"doc-{n}", f"chunk-{n}", n,
            {"kind": "markdown_ast", "start_line": n, "end_line": n + 1}) for n in citations),
    )
    return replace(item, **changes)


def build(*items, max_chars=6000, enabled=True):
    from app.rag.graph_context_builder import build_graph_context
    return build_graph_context(GraphRetrievalResult(tuple(items), enabled=enabled), max_chars=max_chars)


def render(context):
    from app.rag.graph_context_builder import format_graph_context_for_prompt
    return format_graph_context_for_prompt(context)


def test_graph_evidence_is_typed_and_explicitly_serialized():
    raw = evidence(citations=(2, 5))
    context = build(raw)
    item, = context.evidence
    assert context.enabled and context.status == "ok"
    assert item.ref == raw.ref and item.template_name == "table_context_v1"
    assert item.entities == raw.entities and item.relationships == raw.relationships
    assert item.document == raw.document and item.table == raw.table
    assert item.provenance == raw.provenance
    assert item.source_citations == (2, 5)
    payload = json.loads(render(context))
    assert payload["source_citations"] == [2, 5]
    assert payload["graph_id"] == "G" and payload["anchor_id"] == "A"
    assert payload["entities"] == [asdict(entity) for entity in raw.entities]
    assert payload["relationships"] == [asdict(edge) for edge in raw.relationships]
    assert "provenance" not in payload  # explicit prompt projection, not a result dump
    assert "conflicting_refs" not in payload
    assert context.total_chars == len(render(context)) <= 6000


def test_graph_order_is_graph_then_first_citation_then_anchor():
    items = [evidence("Z", "G2", (1,)), evidence("B", "G1", (5,)),
             evidence("A", "G1", (5,)), evidence("C", "G1", (2,))]
    first = build(*items)
    second = build(*reversed(items))
    assert [item.ref.anchor_id for item in first.evidence] == ["C", "A", "B", "Z"]
    assert render(first) == render(second)


def test_provenance_is_complete_deduplicated_and_citation_sorted():
    raw = evidence(citations=(5, 2, 5))
    context = build(raw)
    item, = context.evidence
    assert item.source_citations == (2, 5)
    assert [p.chunk_id for p in item.provenance] == ["chunk-2", "chunk-5"]
    assert item.provenance[0].source_range == raw.provenance[1].source_range
    raw.provenance[1].source_range["start_line"] = 999
    assert item.provenance[0].source_range["start_line"] == 2


@pytest.mark.parametrize("status", ["unavailable", "timeout", "not_found", "ambiguous",
    "unsupported_anchor_type", "conflicting_ref", "invalid_ref", "budget_exhausted",
    "limit_exceeded", "disabled", "truncated"])
def test_non_success_status_never_becomes_fact_evidence(status):
    context = build(evidence(status=status))
    assert context.evidence == () and render(context) == ""
    assert context.diagnostics[0].status == status


def test_partial_success_retains_only_success_and_safe_diagnostics():
    context = build(evidence("A"), evidence("B", status="timeout"), evidence("C", status="not_found"))
    assert [item.ref.anchor_id for item in context.evidence] == ["A"]
    assert [item.status for item in context.diagnostics] == ["success", "timeout", "not_found"]
    assert "timeout" not in render(context) and "not_found" not in render(context)


@pytest.mark.parametrize("change", [{"document": None}, {"table": None}, {"provenance": ()},
    {"template_name": None}, {"provenance": (GraphTriggerProvenance("OTHER", "d", "c", 1),)}])
def test_incomplete_or_unattributed_success_is_not_prompt_evidence(change):
    assert build(evidence(**change)).evidence == ()


@pytest.mark.parametrize("budget", [0, 1, 100, 500, 700, 900, 1200, 6000])
def test_char_budget_never_cuts_json_or_a_record(budget):
    raw = evidence()
    context = build(raw, max_chars=budget)
    text = render(context)
    assert context.total_chars == len(text) <= budget
    for line in text.split("\n\n") if text else ():
        payload = json.loads(line)
        for entity in payload["entities"]:
            assert entity in [asdict(e) for e in raw.entities]
        for edge in payload["relationships"]:
            assert edge in [asdict(e) for e in raw.relationships]
            ids = {entity["id"] for entity in payload["entities"]}
            assert {edge["source_entity_id"], edge["target_entity_id"]} <= ids
    if budget < build(raw).total_chars:
        assert context.was_truncated


def test_exact_budget_keeps_evidence_and_one_less_omits_whole_relationship():
    raw = evidence()
    whole = build(raw)
    assert render(build(raw, max_chars=whole.total_chars)) == render(whole)
    limited = build(raw, max_chars=whole.total_chars - 1)
    assert limited.was_truncated and limited.evidence[0].entities == raw.entities
    assert limited.evidence[0].relationships == ()


def test_oversized_entity_is_not_sliced_and_retained_provenance_is_complete():
    raw = evidence(entities=(replace(evidence().entities[0], name="超长" * 4000),))
    context = build(raw, max_chars=1000)
    assert context.was_truncated
    assert all(not item.entities and not item.relationships for item in context.evidence)
    assert all(item.provenance == raw.provenance for item in context.evidence)


def test_dangling_relationship_is_excluded_even_in_typed_input():
    raw = evidence(relationships=(GraphRelationship("规定", "E1", "MISSING", "G", "D"),))
    context = build(raw)
    assert context.evidence[0].relationships == ()


def test_empty_and_disabled_contexts_have_no_prompt_section():
    for context in (build(), build(evidence(), enabled=False)):
        assert context.evidence == () and context.total_chars == 0 and render(context) == ""
    assert build(evidence(), enabled=False).status == "disabled"


def test_graph_budget_setting_is_independent():
    from app.core.config import Settings
    from pydantic import ValidationError
    settings = Settings(_env_file=None, rag_context_max_chars=1234)
    assert settings.rag_graph_context_max_chars == 6000
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rag_graph_context_max_chars=-1)
