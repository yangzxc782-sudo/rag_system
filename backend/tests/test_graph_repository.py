"""Offline driver protocol checks against the M2 writer-shaped fixtures."""
from copy import deepcopy
import inspect
import json
import re
from unittest.mock import Mock

import pytest
from neo4j import READ_ACCESS
from neo4j.exceptions import Neo4jError
from pydantic import ValidationError

from app.graph.models import GraphRetrievalRequest
from app.graph.templates import resolve_template
from graph_v2_support import settings, binding, row, FakeDriver, repository


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    from neo4j import GraphDatabase
    monkeypatch.setattr(GraphDatabase, "driver", lambda *a, **k: pytest.fail("No real Neo4j"))


@pytest.mark.parametrize("kind", ["table", "clause"])
def test_writer_shaped_roundtrip_and_bound_read_query(kind):
    a = binding(kind=kind)
    repo, driver, factory = repository(FakeDriver([row(a)]))
    result, = repo.fetch_anchor_context(GraphRetrievalRequest((a,)))
    assert result.status == "success"
    assert result.binding == a and len(result.entities) == 2 and len(result.relationships) == 1
    assert result.entities[1].properties["抗拉强度数值"]["unit"] == "MPa"
    query, params = driver.queries[0]
    assert query.text == resolve_template("anchor_context_v2") and query.timeout == 2
    assert params["document_id"] == a.source.document_id and params["graph_build_id"] == a.source.graph_build_id
    assert params["payload_sha256"] == a.source.payload_sha256 and params["anchors"][0]["anchor_type"] == kind
    assert driver.sessions[0]["default_access_mode"] == READ_ACCESS
    assert driver.sessions[0]["disable_auto_commit_retries"] is True
    assert factory.call_args.kwargs["max_transaction_retry_time"] == 0
    assert driver.closed_sessions == 1


def test_no_legacy_or_write_templates():
    query = resolve_template("anchor_context_v2")
    assert all(s in query for s in ("HAS_ENTITY", "MaterialEntity", "RELATES_TO", "relation_type", "ORDER BY", "LIMIT"))
    assert not re.search(r"\b(CREATE|MERGE|DELETE|SET|REMOVE)\b", query)
    assert not any(s in query for s in ("HAS_DOCUMENT", "HAS_TABLE", "CONTAINS_ENTITY", "HAS_TRIPLE", ":Table", ":Entity"))
    assert "b.anchor_id=anchor.anchor_id" in query and "a.graph_build_id=$graph_build_id" in query
    with pytest.raises(ValueError): resolve_template("table_context_v1")


@pytest.mark.parametrize("target,key,value", [
    ("graph","graph_id","foreign"), ("graph","document_id","foreign"),
    ("graph","graph_build_id","foreign"), ("graph","source_version","foreign"),
    ("graph","payload_sha256","foreign"), ("graph","publication_status","staging"),
    ("entity","graph_id","foreign"), ("entity","anchor_id","foreign"),
    ("entity","graph_build_id","foreign"), ("entity","anchor_type","other"),
    ("entity","entity_type","old-kind"), ("edge","graph_id","foreign"),
    ("edge","anchor_id","foreign"), ("edge","graph_build_id","foreign"),
    ("edge","target_entity_id","outside"), ("edge","relation_type","old-kind"),
])
def test_cross_owner_and_invalid_data_fail_closed(target, key, value):
    data = row()
    obj = data["graph"] if target == "graph" else data["entities"][0] if target == "entity" else data["relationships"][0]
    obj[key] = value
    repo, _, _ = repository(FakeDriver([data]))
    assert repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))[0].status == "unavailable"


@pytest.mark.parametrize("raw", ['{"graph_id":"G","graph_id":"other"}', '{"n":NaN}', "[]", "{}",
    '{"x":"' + "a"*66000 + '"}'], ids=["duplicate", "nan", "array", "missing_owner", "over_budget"])
def test_invalid_properties_rejected(raw):
    data = row(); data["entities"][0]["properties_json"] = raw
    repo, _, _ = repository(FakeDriver([data]))
    assert repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))[0].status == "unavailable"


@pytest.mark.parametrize("count,status", [(0,"not_found"),(2,"ambiguous"),(True,"unavailable")])
def test_cardinality(count, status):
    repo, _, _ = repository(FakeDriver([row(candidate_count=count)]))
    assert repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))[0].status == status


def test_cap_is_not_partial_evidence_and_empty():
    repo, _, _ = repository(graph_retrieval_max_entities_per_anchor=1)
    assert repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))[0].status == "truncated"
    repo, _, _ = repository(FakeDriver([]))
    assert repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))[0].status == "not_found"


@pytest.mark.parametrize("error,status", [(TimeoutError("secret"),"timeout"),(RuntimeError("secret"),"unavailable")])
def test_error_normalization_and_cleanup(error, status, caplog):
    repo, driver, _ = repository(FakeDriver(error=error))
    assert repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))[0].status == status
    assert "secret" not in caplog.text and driver.closed_sessions == 1


def test_disabled_lazy_and_lifecycle():
    repo, driver, factory = repository(graph_retrieval_enabled=False)
    assert repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))[0].status == "disabled"
    factory.assert_not_called()
    repo, driver, factory = repository()
    for _ in range(2): repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))
    factory.assert_called_once()
    repo.close(); repo.close()
    assert driver.closes == 1
    assert repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))[0].status == "unavailable"


def test_nonpositive_configuration_and_no_secret_repr():
    assert "unit-secret" not in repr(settings())
    for key in ("graph_retrieval_max_anchors", "graph_retrieval_max_entities_per_anchor", "graph_retrieval_query_timeout_seconds"):
        with pytest.raises(ValidationError): settings(**{key:0})


def test_relationship_projection_retains_properties_and_endpoint_contract():
    query = resolve_template("anchor_context_v2")
    assert "properties(r) +" not in query
    assert "r{.*, source_entity_id:a.entity_id, target_entity_id:b.entity_id}" in query
    assert "(b:MaterialEntity)<-[:HAS_ENTITY]-(g)" in query
    a = binding(kind="clause")
    data = row(a)
    raw = data["relationships"][0]
    props = json.loads(raw["properties_json"])
    props["test_condition"] = {"unit": "MPa", "value": 350, "condition": "synthetic"}
    raw["properties_json"] = json.dumps(props)
    repo, _, _ = repository(FakeDriver([data]))
    result, = repo.fetch_anchor_context(GraphRetrievalRequest((a,)))
    edge, = result.relationships
    assert result.status == "success" and result.binding == a
    assert edge.source_entity_id == raw["source_entity_id"]
    assert edge.target_entity_id == raw["target_entity_id"]
    assert type(edge.source_entity_id) is type(edge.target_entity_id) is str
    assert edge.type == raw["relation_type"]
    assert edge.properties["test_condition"] == props["test_condition"]


@pytest.mark.parametrize("field,limit", [("entities", 200), ("relationships", 400)])
def test_unchanged_production_result_limits_probe_one_extra(field, limit):
    data = row()
    data[field] = data[field][:1] * (limit + 1)
    repo, driver, _ = repository()
    driver.rows = [data]
    result, = repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))
    assert result.status == "truncated"
    assert result.entities == result.relationships == ()
    params = driver.queries[0][1]
    assert params["entity_fetch_limit"] == 201 and params["relationship_fetch_limit"] == 401


@pytest.mark.parametrize("code,status", [
    ("Neo.ClientError.Statement.SyntaxError", "unavailable"),
    ("Neo.ClientError.Transaction.TransactionTimedOut", "timeout"),
    ("Neo.TransientError.Transaction.TransactionTimedOut", "timeout"),
])
def test_safe_query_diagnostics_preserve_status(code, status, caplog, monkeypatch):
    exc = Neo4jError._hydrate_neo4j(code=code, message="SOURCE_SECRET properties_json Authorization PASSWORD")
    repo, driver, _ = repository(FakeDriver(error=exc))
    monkeypatch.setattr("app.graph.repository.time.monotonic", Mock(side_effect=[10, 10.125]))
    result, = repo.fetch_anchor_context(GraphRetrievalRequest((binding(),), remaining_budget_ms=4000))
    assert result.status == status
    record, = [r for r in caplog.records if getattr(r, "event", None) == "graph_retrieval_failure"]
    d = record.graph_diagnostics
    assert d["phase"] == "query" and d["reason"] == "neo4j_query_error"
    assert d["template_name"] == "anchor_context_v2" and d["neo4j_code"] == code
    assert d["exception_type"] == type(exc).__name__
    assert d["anchor_count"] == 1 and d["max_anchor_count"] == 8
    assert re.fullmatch("[0-9a-f]{32}", d["batch_id"])
    assert d["elapsed_ms"] == 125 and d["remaining_budget_ms"] == 3875
    assert driver.queries[0][0].timeout == 2
    assert record.exc_info is None
    for secret in ("SOURCE_SECRET", "properties_json", "Authorization", "PASSWORD", "unit-secret", "kg/source"):
        assert secret not in caplog.text and secret not in repr(d)
    assert code in caplog.text and "anchor_context_v2" in caplog.text


@pytest.mark.parametrize("change,reason", [
    ("graph_owner", "binding_mismatch"),
    ("endpoint", "relationship_endpoint_mismatch"),
    ("properties", "neo4j_decode_error"),
])
def test_decode_failure_has_separate_safe_diagnostics(change, reason, caplog):
    data = row()
    if change == "graph_owner": data["graph"]["graph_build_id"] = "SOURCE_SECRET"
    elif change == "endpoint": data["relationships"][0]["target_entity_id"] = "SOURCE_SECRET"
    else: data["entities"][0]["properties_json"] = '{"SOURCE_SECRET":'
    repo, _, _ = repository(FakeDriver([data]))
    assert repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))[0].status == "unavailable"
    d = next(r.graph_diagnostics for r in caplog.records if getattr(r, "event", None) == "graph_retrieval_failure")
    assert d["phase"] == "decode" and d["reason"] == reason
    assert d["neo4j_code"] is None and d["anchor_index"] == 0
    assert d["remaining_budget_ms"] is None  # Direct repository call has no dispatch deadline.
    assert "SOURCE_SECRET" not in caplog.text


def test_missing_entity_membership_cannot_be_replaced_by_edge_endpoint():
    data = row()
    data["entities"] = data["entities"][:1]
    repo, _, _ = repository(FakeDriver([data]))
    result, = repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))
    assert result.status == "unavailable" and not result.relationships


def test_unreadable_error_metadata_cannot_hide_original_failure(caplog):
    class BrokenMetadata(RuntimeError):
        @property
        def code(self): raise ValueError("PASSWORD")
    repo, _, _ = repository(FakeDriver(error=BrokenMetadata("SOURCE_SECRET")))
    assert repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))[0].status == "unavailable"
    d = next(r.graph_diagnostics for r in caplog.records if getattr(r, "event", None) == "graph_retrieval_failure")
    assert d["neo4j_code"] is None and d["exception_type"] == "BrokenMetadata"
    assert "PASSWORD" not in caplog.text and "SOURCE_SECRET" not in caplog.text


def test_lazy_result_failure_is_observable_without_logging_rows(caplog):
    from graph_v2_support import FakeSession
    class LazySession(FakeSession):
        def run(self, query, parameters):
            yield row()
            raise RuntimeError("SOURCE_SECRET properties_json")
    driver = FakeDriver()
    driver.session = lambda **kwargs: LazySession(driver)
    repo, _, _ = repository(driver)
    assert repo.fetch_anchor_context(GraphRetrievalRequest((binding(),)))[0].status == "unavailable"
    d = next(r.graph_diagnostics for r in caplog.records if getattr(r, "event", None) == "graph_retrieval_failure")
    assert d["phase"] == "read_result" and d["reason"] == "neo4j_query_error"
    assert d["exception_type"] == "RuntimeError" and d["neo4j_code"] is None
    assert "SOURCE_SECRET" not in caplog.text and "properties_json" not in caplog.text
