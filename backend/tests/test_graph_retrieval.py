from dataclasses import replace
from unittest.mock import Mock
import pytest

from app.graph.models import GraphAnchorResult
from app.services.graph_retrieval import GraphRetrievalService
from graph_v2_support import binding, settings, FixtureAuthority


def service(**changes):
    repo = Mock()
    repo.fetch_anchor_context.side_effect = lambda req: tuple(GraphAnchorResult(a, "not_found") for a in req.anchors)
    return GraphRetrievalService(settings(**changes), repository=repo, authority=FixtureAuthority()), repo


def test_query_dedup_preserves_each_chunk_provenance():
    a = binding()
    b = replace(a, provenance=(replace(a.provenance[0], chunk_id="another", citation_id=2),))
    s, repo = service()
    result = s.retrieve((a,b,a))
    repo.fetch_anchor_context.assert_called_once()
    assert len(result.items) == 1 and len(result.items[0].binding.provenance) == 2


def test_conflicting_identity_never_queries():
    a = binding(); b = replace(a, source=replace(a.source, source_end=21))
    s, repo = service()
    assert s.retrieve((a,b)).items[0].status == "conflicting_ref"
    repo.fetch_anchor_context.assert_not_called()


def test_group_owner_conflict_and_malicious_result_rejected():
    s, repo = service()
    a, b = binding(), binding(2)
    b = replace(b, source=replace(b.source, document_id="other"))
    assert all(r.status == "unavailable" for r in s.retrieve((a,b)).items)
    repo.fetch_anchor_context.assert_not_called()
    repo.fetch_anchor_context.side_effect = lambda req: (GraphAnchorResult(binding(graph="other"), "success"),)
    assert s.retrieve((a,)).items[0].status == "unavailable"


def test_limits_disabled_and_total_budget():
    s, repo = service(graph_retrieval_max_anchors=1)
    assert [r.status for r in s.retrieve((binding(), binding(2))).items] == ["not_found","limit_exceeded"]
    s, repo = service(graph_retrieval_enabled=False)
    assert s.retrieve((binding(),)).items[0].status == "disabled"
    repo.fetch_anchor_context.assert_not_called()
    s, repo = service()
    s._clock = Mock(side_effect=[0, 0, 9])
    result = s.retrieve((binding(), binding(2,graph="H")))
    assert [r.status for r in result.items] == ["not_found","budget_exhausted"]
    repo.fetch_anchor_context.assert_called_once()


def test_service_exception_is_diagnostic_and_raw_refs_rejected():
    s, repo = service()
    repo.fetch_anchor_context.side_effect = RuntimeError("secret")
    assert s.retrieve((binding(),)).items[0].status == "unavailable"
    with pytest.raises(TypeError): s.retrieve((binding().ref,))


def test_ten_mapped_anchors_admit_eight_in_one_batch_without_budget_changes():
    from graph_v2_support import FakeDriver, repository, row
    from app.rag.graph_context_builder import build_graph_context
    anchors = tuple(binding(n, kind="clause" if n % 2 else "table") for n in range(1, 11))
    repo, driver, _ = repository(FakeDriver([row(a) for a in anchors[:8]]))
    config = settings()
    assert (config.graph_retrieval_max_anchors, config.graph_retrieval_max_entities_per_anchor,
        config.graph_retrieval_max_relationships_per_anchor, config.graph_retrieval_query_timeout_seconds,
        config.graph_retrieval_total_budget_seconds) == (8, 200, 400, 2, 5)
    s = GraphRetrievalService(config, repository=repo, clock=Mock(side_effect=[0, 1]))
    result = s.retrieve(anchors)
    assert [r.status for r in result.items] == ["success"] * 8 + ["limit_exceeded"] * 2
    assert len(driver.queries) == 1
    query, params = driver.queries[0]
    assert [a["anchor_id"] for a in params["anchors"]] == [a.ref.anchor_id for a in anchors[:8]]
    assert query.timeout == 2
    graph = build_graph_context(result, max_chars=0)
    assert len(graph.diagnostics) == 10 and all(d.mapped for d in graph.diagnostics)
    assert all(not d.facts_used for d in graph.diagnostics)
    assert sum(d.use_status == "graph_budget" for d in graph.diagnostics) == 8
    assert sum(d.query_status == "limit_exceeded" for d in graph.diagnostics) == 2


def test_remaining_dispatch_budget_is_observation_only():
    s, repo = service()
    s._clock = Mock(side_effect=[0, 4.75])
    s.retrieve((binding(),))
    request = repo.fetch_anchor_context.call_args.args[0]
    assert request.remaining_budget_ms == 250
    assert s._settings.graph_retrieval_query_timeout_seconds == 2
