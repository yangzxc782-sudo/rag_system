from dataclasses import replace
from copy import deepcopy
import json
import pytest
from app.graph.models import GraphRetrievalRequest, GraphRetrievalResult
from app.rag.graph_context_builder import build_graph_context, format_graph_context_for_prompt
from graph_v2_support import binding, repository, FakeDriver, row, nested_properties, reorder_objects, settings
from app.rag.context_builder import RagContext
from app.rag.conversation_prompt import checked_request, prompt_fingerprint, prompt_cost
from app.rag.history_budget import HistoryContext
from app.schemas.conversation_rag import GraphPayload


def evidence(a=None, **changes):
    a = a or binding()
    repo, _, _ = repository(FakeDriver([row(a)]))
    return replace(repo.fetch_anchor_context(GraphRetrievalRequest((a,)))[0], **changes)


@pytest.mark.parametrize("kind", ["table","clause"])
def test_full_unit_evidence_has_real_source_and_value_objects(kind):
    graph = build_graph_context(GraphRetrievalResult((evidence(binding(kind=kind)),)))
    assert graph.evidence[0].ref.anchor_type == kind
    assert graph.diagnostics[0].facts_used
    text = format_graph_context_for_prompt(graph)
    assert "350" in text and "MPa" in text and "document_id" in text
    assert "source_path" not in text and '"document":' not in text and '"table":' not in text


@pytest.mark.parametrize("ranges,covered", [
    ([(0,10),(10,20)],True), ([(0,12),(8,20)],True),
    ([(0,9),(10,20)],False), ([(0,10)],False), ([(3,20)],False),
    ([(0,20)],True), ([(0,10),(10,10)],False),
])
def test_continuous_unit_coverage_no_gap_envelope(ranges, covered):
    a = binding()
    a = replace(a, provenance=tuple(replace(a.provenance[0], chunk_id=str(i), citation_id=i+1,
        effective_start=s,effective_end=e) for i,(s,e) in enumerate(ranges)))
    graph = build_graph_context(GraphRetrievalResult((evidence(a),)))
    assert a.fully_covered == covered
    assert bool(graph.evidence) == covered
    diagnostic = graph.diagnostics[0]
    assert diagnostic.mapped and diagnostic.query_status == "success"
    assert diagnostic.facts_used == covered


def test_budget_drops_complete_evidence_not_values_or_conditions():
    raw = GraphRetrievalResult((evidence(),))
    complete = build_graph_context(raw)
    small = build_graph_context(raw, max_chars=complete.total_chars-1)
    assert not small.evidence and small.was_truncated and small.diagnostics[0].use_status == "graph_budget"
    assert build_graph_context(raw,max_chars=complete.total_chars).evidence == complete.evidence


@pytest.mark.parametrize("status", ["truncated","timeout","not_found","unavailable"])
def test_non_success_is_diagnostic_only(status):
    graph = build_graph_context(GraphRetrievalResult((evidence(status=status),)))
    assert not graph.evidence and not format_graph_context_for_prompt(graph)
    assert graph.diagnostics[0].query_status == status


def property_result(properties, n=1):
    item = evidence(binding(n))
    return replace(item,
        entities=tuple(replace(e, properties=deepcopy(properties)) for e in item.entities),
        relationships=tuple(replace(r, properties=deepcopy(properties)) for r in item.relationships))


def property_prompt(graph):
    return checked_request("q", "q", HistoryContext(), RagContext("q", "no_context", [], 0, 0),
        graph, settings(conversation_answer_max_input_tokens=100000))


def test_property_object_order_has_identical_text_messages_hash_and_budget():
    props = nested_properties()
    first = GraphRetrievalResult((property_result(props), property_result(props, 2)))
    reordered = GraphRetrievalResult((property_result(reorder_objects(props)),
                                     property_result(reorder_objects(props), 2)))
    one_size = build_graph_context(GraphRetrievalResult(first.items[:1]), max_chars=100000).total_chars
    for budget in (one_size - 1, one_size, 100000):
        a, b = (build_graph_context(result, max_chars=budget) for result in (first, reordered))
        assert format_graph_context_for_prompt(a) == format_graph_context_for_prompt(b)
        assert a.total_chars == b.total_chars
        assert a.diagnostics == b.diagnostics
        assert [e.ref for e in a.evidence] == [e.ref for e in b.evidence]
        ra, rb = property_prompt(a), property_prompt(b)
        assert ra.messages == rb.messages
        assert prompt_cost(ra.messages) == prompt_cost(rb.messages)
        assert prompt_fingerprint(ra.messages) == prompt_fingerprint(rb.messages)


def test_property_copies_preserve_scalars_and_input_insertion_order():
    from app.rag.graph_context_builder import _canonical_properties
    props = nested_properties()
    before = json.dumps(props, ensure_ascii=False)
    canonical = _canonical_properties(props)
    assert list(canonical) == sorted(props)
    assert json.dumps(canonical, ensure_ascii=False) == json.dumps(props, ensure_ascii=False, sort_keys=True)
    assert _canonical_properties(canonical) == canonical

    def check(original, copied):
        assert type(original) is type(copied)
        if isinstance(original, dict):
            assert original is not copied
            for key in original: check(original[key], copied[key])
        elif isinstance(original, list):
            assert original is not copied and len(original) == len(copied)
            for a, b in zip(original, copied): check(a, b)
        else:
            assert original == copied
    check(props, canonical)
    graph = build_graph_context(GraphRetrievalResult((property_result(props),)), max_chars=100000)
    snapshot = GraphPayload(evidence=graph.evidence[0])
    before_snapshot = snapshot.model_dump_json()
    before_graph = repr(graph)
    text = format_graph_context_for_prompt(graph)
    assert text == format_graph_context_for_prompt(graph)
    decoded = json.loads(text)
    for value in (*decoded["entities"], *decoded["relationships"]):
        check(props, value["properties"])
    assert json.dumps(props, ensure_ascii=False) == before
    assert snapshot.model_dump_json() == before_snapshot and repr(graph) == before_graph


def test_property_array_order_and_actual_values_remain_fingerprint_significant():
    props = nested_properties()
    original = build_graph_context(GraphRetrievalResult((property_result(props),)), max_chars=100000)
    for key, value in (("items", list(reversed(props["items"]))), ("number", 2),
                       ("provenance", {"unit": "Pa", "condition": {"z": "cold", "a": "cast"}})):
        changed = build_graph_context(GraphRetrievalResult((property_result({**props, key: value}),)), max_chars=100000)
        assert format_graph_context_for_prompt(original) != format_graph_context_for_prompt(changed)
        assert prompt_fingerprint(property_prompt(original).messages) != prompt_fingerprint(property_prompt(changed).messages)


def test_outer_layout_evidence_entities_relationships_and_citations_keep_order():
    graph = build_graph_context(GraphRetrievalResult((property_result(nested_properties()),)), max_chars=100000)
    item = graph.evidence[0]
    second = replace(item, entities=tuple(reversed(item.entities)),
        relationships=(*item.relationships, replace(item.relationships[0], id="second")))
    graph = replace(graph, evidence=(second, item))
    parsed = [json.loads(line) for line in format_graph_context_for_prompt(graph).split("\n\n")]
    assert list(parsed[0]) == ["schema_version", "template", "anchor", "source", "source_citations", "entities", "relationships"]
    for payload, source in zip(parsed, graph.evidence):
        assert payload["source_citations"] == list(source.source_citations)
        assert [e["id"] for e in payload["entities"]] == [e.id for e in source.entities]
        assert [r["id"] for r in payload["relationships"]] == [r.id for r in source.relationships]
        assert list(payload["entities"][0]) == ["id", "name", "entity_type", "properties"]
        assert list(payload["relationships"][0]) == ["id", "type", "source_entity_id", "target_entity_id", "properties"]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_properties_still_rejected(invalid):
    with pytest.raises(ValueError):
        build_graph_context(GraphRetrievalResult((property_result({"value": invalid}),)))


@pytest.mark.parametrize("props", [{1: "not a string key"}, {"nested": [{None: 1}]}])
def test_invalid_property_keys_are_not_coerced(props):
    with pytest.raises(TypeError):
        build_graph_context(GraphRetrievalResult((property_result(props),)))
