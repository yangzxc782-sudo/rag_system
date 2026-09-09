from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.graph.models import KGRef
from test_graph_repository import no_network, ref, settings


def service(repo=None, clock=None, **overrides):
    from app.services.graph_retrieval import GraphRetrievalService
    from app.graph.models import GraphAnchorResult
    repo = repo or Mock()
    if not repo.fetch_table_context.side_effect:
        repo.fetch_table_context.side_effect = lambda request: tuple(
            GraphAnchorResult(ref=anchor, status="not_found") for anchor in request.anchors)
    return GraphRetrievalService(settings(**overrides), repository=repo,
                                 **({"clock": clock} if clock else {})), repo


def test_no_refs_no_repository_call():
    graph, repo = service()
    assert graph.retrieve([]).items == ()
    repo.fetch_table_context.assert_not_called()


def test_feature_disabled_returns_empty_safe_without_repository():
    graph, repo = service(graph_retrieval_enabled=False, neo4j_uri=None, neo4j_password=None)
    result = graph.retrieve([ref()])
    assert result.enabled is False
    assert result.items[0].status == "disabled"
    repo.fetch_table_context.assert_not_called()


def test_identical_refs_dedupe_and_provenance_is_retained():
    from app.graph.models import GraphTriggerProvenance
    graph, repo = service()
    first = GraphTriggerProvenance(anchor_id="A", document_id="doc", chunk_id="c1", citation_id=1)
    second = replace(first, chunk_id="c2", citation_id=2)
    result = graph.retrieve([ref(), ref()], provenance=[first, second, first])
    repo.fetch_table_context.assert_called_once()
    request = repo.fetch_table_context.call_args.args[0]
    assert request.anchors == (ref(),)
    assert request.provenance == (first, second)
    assert result.items[0].provenance == (first, second)


@pytest.mark.parametrize("changed", [ref(graph="H"), ref(kind="section"), ref(table="T2")])
def test_conflicting_anchor_payload_is_not_queried(changed):
    graph, repo = service()
    result = graph.retrieve([ref(), changed, ref()])
    assert result.items[0].status == "conflicting_ref"
    assert result.items[0].conflicting_refs == (ref(), changed)
    repo.fetch_table_context.assert_not_called()


def test_multi_graph_grouping_retains_original_anchor_order():
    graph, repo = service()
    anchors = [ref("A", "G2"), ref("B", "G1"), ref("C", "G2")]
    result = graph.retrieve(anchors)
    requests = [call.args[0] for call in repo.fetch_table_context.call_args_list]
    assert [(r.graph_id, r.anchors) for r in requests] == [("G2", (anchors[0], anchors[2])), ("G1", (anchors[1],))]
    assert [item.ref for item in result.items] == anchors


def test_request_rejects_mixed_graph_ids():
    from app.graph.models import GraphRetrievalRequest
    with pytest.raises(ValueError):
        GraphRetrievalRequest(graph_id="G", anchors=(ref(), ref("B", "H")))


def test_anchor_limit_applies_after_dedupe_and_reports_omitted():
    graph, repo = service(graph_retrieval_max_anchors=1)
    result = graph.retrieve([ref(), ref(), ref("B")])
    assert [item.status for item in result.items] == ["not_found", "limit_exceeded"]
    assert repo.fetch_table_context.call_args.args[0].anchors == (ref(),)


@pytest.mark.parametrize("anchor,status", [(ref(kind="section", table=None), "unsupported_anchor_type"),
    (ref(table=None), "invalid_ref"), (ref(table=""), "invalid_ref"),
    (ref(graph=""), "invalid_ref"), (ref(anchor=""), "invalid_ref")])
def test_invalid_or_unsupported_refs_never_query(anchor, status):
    graph, repo = service()
    result = graph.retrieve([anchor])
    assert result.items[0].status == status
    repo.fetch_table_context.assert_not_called()


def test_conflict_detection_precedes_limit():
    graph, repo = service(graph_retrieval_max_anchors=1)
    result = graph.retrieve([ref(), ref("B"), ref(table="T2")])
    assert [item.status for item in result.items] == ["conflicting_ref", "not_found"]
    assert repo.fetch_table_context.call_args.args[0].anchors == (ref("B"),)


def test_partial_graph_failure_does_not_discard_success():
    from app.graph.models import GraphAnchorResult
    repo = Mock()
    repo.fetch_table_context.side_effect = [
        (GraphAnchorResult(ref=ref(), status="success"),), RuntimeError("credential leak")]
    graph, repo = service(repo)
    result = graph.retrieve([ref(), ref("B", "H")])
    assert [item.status for item in result.items] == ["success", "unavailable"]
    assert "credential" not in repr(result)


def test_partial_anchor_failure_and_repository_order_are_preserved():
    from app.graph.models import GraphAnchorResult
    repo = Mock()
    repo.fetch_table_context.side_effect = lambda request: (
        GraphAnchorResult(ref=ref("B"), status="timeout"), GraphAnchorResult(ref=ref(), status="success"))
    graph, _ = service(repo)
    result = graph.retrieve([ref(), ref("B")])
    assert [item.status for item in result.items] == ["success", "timeout"]


def test_total_budget_stops_next_group_without_interrupting_completed_work():
    ticks = iter([10.0, 10.1, 15.1])
    graph, repo = service(clock=lambda: next(ticks))
    result = graph.retrieve([ref(), ref("B", "H"), ref("C", "J")])
    assert [item.status for item in result.items] == ["not_found", "budget_exhausted", "budget_exhausted"]
    repo.fetch_table_context.assert_called_once()


def test_repository_cannot_inject_result_for_wrong_ref():
    from app.graph.models import GraphAnchorResult
    repo = Mock()
    repo.fetch_table_context.side_effect = lambda request: (GraphAnchorResult(ref=ref(graph="OTHER"), status="success"),)
    graph, _ = service(repo)
    assert graph.retrieve([ref()]).items[0].status == "unavailable"
