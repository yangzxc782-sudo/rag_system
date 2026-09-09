from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import inspect
import json
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from neo4j import Query, READ_ACCESS
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.graph.models import KGRef


def settings(**overrides):
    values = dict(graph_retrieval_enabled=True, neo4j_uri="bolt://graph.example.invalid:7687",
                  neo4j_database="neo4j", neo4j_username="graph_reader",
                  neo4j_password=SecretStr("unit-secret"), document_deletion_executor_enabled=False,
                  llm_provider="local", llm_model="test", llm_base_url="http://localhost:11434/v1")
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    from neo4j import GraphDatabase
    def forbidden(*args, **kwargs):
        pytest.fail("M5 unit tests must not connect to a graph server")
    # Do not block Windows asyncio's local socketpair used by TestClient.
    monkeypatch.setattr(GraphDatabase, "driver", forbidden)


def ref(anchor="A", graph="G", table="T-P8-1", kind="table"):
    return KGRef(anchor, graph, kind, table)


def request(*refs):
    from app.graph.models import GraphRetrievalRequest
    return GraphRetrievalRequest(graph_id=refs[0].graph_id, anchors=tuple(refs))


def entity(identity, **overrides):
    result = dict(id=identity, name=f"实体 {identity} ≥720", entity_type="工艺",
                  doc_id="D", table_ref="T-P8-1", page=8)
    result.update(overrides)
    return result


def edge(source="E1", target="E2", **overrides):
    result = dict(type="规定", source_entity_id=source, target_entity_id=target,
                  graph_id="G", doc_id="D")
    result.update(overrides)
    return result


def row(anchor="A", **overrides):
    result = dict(anchor_id=anchor, graph_id="G", candidate_count=1, shared_document=False,
                  document=dict(doc_id="D", source_pdf="source.pdf", source_type="pdf"),
                  table=dict(table_id="D_T-P8-1", table_ref="T-P8-1", page=8, table_index=1),
                  entities=[entity("E2"), entity("E1")], relationships=[edge()])
    result.update(overrides)
    return result


class FakeSession:
    def __init__(self, driver):
        self.driver = driver
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.driver.closed_sessions += 1
    def run(self, query, parameters):
        self.driver.queries.append((query, deepcopy(parameters)))
        if self.driver.error:
            raise self.driver.error
        return iter(deepcopy(self.driver.rows))


class FakeDriver:
    def __init__(self, rows=None, error=None):
        self.rows = rows if rows is not None else [row()]
        self.error = error
        self.queries, self.sessions = [], []
        self.closed_sessions = self.closes = 0
    def session(self, **kwargs):
        self.sessions.append(kwargs)
        return FakeSession(self)
    def close(self):
        self.closes += 1


def repository(driver=None, **overrides):
    from app.graph.repository import Neo4jRepository
    driver = driver or FakeDriver()
    factory = Mock(return_value=driver)
    return Neo4jRepository(settings(**overrides), driver_factory=factory), driver, factory


def test_graph_config_defaults_disabled_and_missing_credentials_allowed():
    config = Settings(_env_file=None)
    assert config.graph_retrieval_enabled is False
    assert config.neo4j_uri is None and config.neo4j_password is None
    assert config.graph_retrieval_max_anchors == 8
    assert config.graph_retrieval_max_entities_per_anchor == 200
    assert config.graph_retrieval_max_relationships_per_anchor == 400
    assert config.graph_retrieval_query_timeout_seconds == 2
    assert config.graph_retrieval_total_budget_seconds == 5
    assert config.neo4j_connection_timeout_seconds == 1
    assert config.neo4j_connection_acquisition_timeout_seconds == 1
    assert isinstance(settings().neo4j_password, SecretStr)
    assert "unit-secret" not in repr(settings())


@pytest.mark.parametrize("field", ["graph_retrieval_max_anchors", "graph_retrieval_max_entities_per_anchor",
    "graph_retrieval_max_relationships_per_anchor", "graph_retrieval_query_timeout_seconds",
    "graph_retrieval_total_budget_seconds", "neo4j_connection_timeout_seconds",
    "neo4j_connection_acquisition_timeout_seconds"])
def test_nonpositive_limits_are_rejected(field):
    with pytest.raises(ValidationError):
        settings(**{field: 0})


@pytest.mark.parametrize("uri", ["https://graph.invalid", "bolt://user:password@graph.invalid", "bolt://graph.invalid/?secret=x"])
def test_invalid_uri_rejected(uri):
    with pytest.raises(ValidationError):
        settings(neo4j_uri=uri)


def test_disabled_repository_never_constructs_driver():
    repo, driver, factory = repository(graph_retrieval_enabled=False, neo4j_uri=None, neo4j_password=None)
    assert repo.fetch_table_context(request(ref()))[0].status == "disabled"
    repo.close()
    factory.assert_not_called()
    assert driver.closes == 0


@pytest.mark.parametrize("missing", ["neo4j_uri", "neo4j_database", "neo4j_username", "neo4j_password"])
def test_missing_runtime_config_is_unavailable_without_driver(missing):
    repo, driver, factory = repository(**{missing: None})
    assert repo.fetch_table_context(request(ref()))[0].status == "unavailable"
    factory.assert_not_called()


def test_driver_is_lazy_reused_read_only_timed_and_closed_once():
    repo, driver, factory = repository()
    factory.assert_not_called()
    for _ in range(2):
        result = repo.fetch_table_context(request(ref()))
        assert result[0].status == "success"
    factory.assert_called_once()
    _, kwargs = factory.call_args
    assert kwargs["connection_timeout"] == kwargs["connection_acquisition_timeout"] == 1
    assert kwargs["max_transaction_retry_time"] == 0
    assert driver.closed_sessions == 2
    assert all(session["default_access_mode"] == READ_ACCESS for session in driver.sessions)
    assert all(session["database"] == "neo4j" for session in driver.sessions)
    assert all(session["disable_auto_commit_retries"] is True for session in driver.sessions)
    assert all(isinstance(query, Query) and query.timeout == 2 for query, _ in driver.queries)
    repo.close()
    repo.close()
    assert driver.closes == 1
    assert repo.fetch_table_context(request(ref()))[0].status == "unavailable"
    assert len(driver.queries) == 2


def test_close_before_first_use_is_idempotent():
    repo, driver, factory = repository()
    repo.close()
    repo.close()
    factory.assert_not_called()
    assert driver.closes == 0


def test_fixed_template_and_parameter_binding():
    from app.graph.templates import resolve_template
    repo, driver, _ = repository(FakeDriver(rows=[]))
    hostile = ref("A' RETURN 1 //", graph="G' MATCH (n) //", table="T' RETURN n //")
    repo.fetch_table_context(request(hostile))
    query, params = driver.queries[0]
    assert query.text == resolve_template("table_context_v1")
    assert hostile.graph_id not in query.text and hostile.table_ref not in query.text
    assert params["graph_id"] == hostile.graph_id
    assert params["anchors"] == [{"anchor_id": hostile.anchor_id, "table_ref": hostile.table_ref}]
    assert params["entity_fetch_limit"] == 201 and params["relationship_fetch_limit"] == 401
    with pytest.raises(ValueError):
        resolve_template("MATCH (n) RETURN n")
    assert not hasattr(repo, "execute_arbitrary_cypher") and not hasattr(repo, "run")
    assert "query" not in inspect.signature(repo.fetch_table_context).parameters


def test_cypher_locks_audited_path_isolation_and_bounded_subqueries():
    from app.graph.templates import resolve_template, RELATIONSHIP_TYPES
    query = resolve_template("table_context_v1")
    assert set(RELATIONSHIP_TYPES) == {"含有", "规定", "适用于", "约束", "替代", "对应"}
    assert not re.search(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|LOAD|APOC|DBMS)\b", query, re.I)
    for fragment in ("(g:KnowledgeGraph {graph_id: $graph_id})", "UNWIND $anchors AS ref",
                     "(g)-[:HAS_DOCUMENT]->(d:Document)-[:HAS_TABLE]->(t:Table",
                     "LIMIT 2", "other <> g", "(other:KnowledgeGraph)-[:HAS_DOCUMENT]->(d)",
                     "e.doc_id = d.doc_id", "e.table_ref = t.table_ref",
                     "s.doc_id = d.doc_id", "target.doc_id = d.doc_id",
                     "s.table_ref = t.table_ref", "target.table_ref = t.table_ref",
                     "r.graph_id = $graph_id", "r.doc_id = d.doc_id",
                     "LIMIT $entity_fetch_limit", "LIMIT $relationship_fetch_limit",
                     "ORDER BY e.id", "ORDER BY source_entity_id, relation_type, target_entity_id"):
        assert fragment in query
    assert "(t)-[:CONTAINS_ENTITY]->(s:Entity)" in query
    assert "(target:Entity)" in query
    assert "EXISTS { MATCH (t)-[:CONTAINS_ENTITY]->(target) }" in query
    assert all(f"`{kind}`" in query for kind in RELATIONSHIP_TYPES)


@pytest.mark.parametrize("rows,status", [([], "not_found"), ([row(candidate_count=0)], "not_found"),
    ([row(candidate_count=2)], "ambiguous"), ([row(shared_document=True)], "ambiguous")])
def test_missing_ambiguous_and_shared_targets_return_no_context(rows, status):
    repo, _, _ = repository(FakeDriver(rows=rows))
    item, = repo.fetch_table_context(request(ref()))
    assert item.status == status
    assert item.document is item.table is None
    assert item.entities == item.relationships == ()


@pytest.mark.parametrize("corrupt", ["entity_doc", "entity_table", "relation_type", "relation_graph",
    "relation_doc", "unknown_source", "unknown_target"])
def test_foreign_entities_and_edges_are_excluded_at_typed_boundary(corrupt):
    raw = row()
    if corrupt.startswith("entity"):
        raw["entities"].append(entity("FOREIGN", **{"doc_id" if corrupt == "entity_doc" else "table_ref": "OTHER"}))
        raw["relationships"].append(edge(target="FOREIGN"))
    else:
        field = {"relation_type": "type", "relation_graph": "graph_id", "relation_doc": "doc_id",
                 "unknown_source": "source_entity_id", "unknown_target": "target_entity_id"}[corrupt]
        raw["relationships"].append(edge(**{field: "OTHER"}))
    repo, _, _ = repository(FakeDriver(rows=[raw]))
    item, = repo.fetch_table_context(request(ref()))
    assert item.status == "success"
    assert [node.id for node in item.entities] == ["E1", "E2"]
    assert [asdict(relation) for relation in item.relationships] == [edge()]


def test_sorting_typed_values_and_no_numeric_inference():
    raw = row(relationships=[edge("E2", "E1"), edge(), edge(type="含有")])
    repo, _, _ = repository(FakeDriver(rows=[raw]))
    item, = repo.fetch_table_context(request(ref()))
    assert [node.id for node in item.entities] == ["E1", "E2"]
    assert [(r.source_entity_id, r.type, r.target_entity_id) for r in item.relationships] == [
        ("E1", "含有", "E2"), ("E1", "规定", "E2"), ("E2", "规定", "E1")]
    assert item.entities[0].name == "实体 E1 ≥720"
    assert set(asdict(item.entities[0])) == {"id", "name", "entity_type", "doc_id", "table_ref", "page"}
    json.dumps(asdict(item), ensure_ascii=False)
    assert item.template_name == "table_context_v1"


def test_entity_truncation_excludes_edges_to_omitted_entities():
    repo, driver, _ = repository(FakeDriver(rows=[row(entities=[entity("E3"), entity("E1"), entity("E2")],
        relationships=[edge(), edge(target="E3")])]), graph_retrieval_max_entities_per_anchor=2)
    item, = repo.fetch_table_context(request(ref()))
    assert item.status == "truncated" and item.truncation.entities is True
    assert item.truncation.relationships_limited_by_entities is True
    assert [e.id for e in item.entities] == ["E1", "E2"]
    assert [asdict(r) for r in item.relationships] == [edge()]
    assert driver.queries[0][1]["entity_fetch_limit"] == 3


def test_relationship_truncation_keeps_whole_sorted_records():
    repo, _, _ = repository(FakeDriver(rows=[row(relationships=[edge("E2", "E1"), edge()])]),
                            graph_retrieval_max_relationships_per_anchor=1)
    item, = repo.fetch_table_context(request(ref()))
    assert item.status == "truncated" and item.truncation.relationships is True
    assert [asdict(r) for r in item.relationships] == [edge()]


@pytest.mark.parametrize("error,status", [(TimeoutError("secret"), "timeout"),
    (Neo4jError._hydrate_neo4j(code="Neo.ClientError.Transaction.TransactionTimedOut", message="secret"), "timeout"),
    (ServiceUnavailable("secret"), "unavailable")])
def test_driver_failure_is_typed_safe_and_not_retried(error, status, caplog):
    repo, driver, _ = repository(FakeDriver(error=error))
    item, = repo.fetch_table_context(request(ref()))
    assert item.status == status
    assert len(driver.queries) == driver.closed_sessions == 1
    assert "secret" not in repr(item) + caplog.text


def test_malformed_anchor_row_does_not_discard_other_anchor():
    repo, _, _ = repository(FakeDriver(rows=[row("A", document={}), row("B")]))
    items = repo.fetch_table_context(request(ref(), ref("B")))
    assert [item.status for item in items] == ["unavailable", "success"]


@pytest.mark.parametrize("change", [{"graph_id": "OTHER"}, {"shared_document": None},
    {"table": {"table_id": "OTHER", "table_ref": "OTHER"}}])
def test_unproven_result_identity_fails_closed(change):
    repo, _, _ = repository(FakeDriver(rows=[row(**change)]))
    item, = repo.fetch_table_context(request(ref()))
    assert item.status == "unavailable" and not item.entities


@pytest.mark.parametrize("enabled,missing", [(False, True), (True, True), (True, False)])
def test_startup_never_constructs_or_pings_driver(monkeypatch, enabled, missing):
    from app.main import create_app
    from neo4j import GraphDatabase
    factory = Mock(side_effect=ServiceUnavailable("unreachable"))
    monkeypatch.setattr(GraphDatabase, "driver", factory)
    app = create_app(settings=settings(graph_retrieval_enabled=enabled,
                                      neo4j_password=None if missing else SecretStr("unit-secret")))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    factory.assert_not_called()


def test_lifespan_closes_created_graph_driver_and_keeps_llm_cleanup(monkeypatch):
    import app.main as main
    from neo4j import GraphDatabase
    driver = FakeDriver()
    monkeypatch.setattr(GraphDatabase, "driver", Mock(return_value=driver))
    cleanup = Mock()
    monkeypatch.setattr(main, "clear_llm_provider_cache", cleanup)
    app = main.create_app(settings=settings())
    with TestClient(app):
        result = app.state.graph_repository.fetch_table_context(request(ref()))
        assert result[0].status == "success"
        assert driver.closes == 0
    cleanup.assert_called_once()
    assert driver.closes == 1


@pytest.mark.parametrize("anchor", [ref(kind="section", table=None), ref(table=None), ref(table=" ")])
def test_repository_defends_direct_unsupported_requests(anchor):
    repo, driver, factory = repository()
    assert repo.fetch_table_context(request(anchor))[0].status in {"unsupported_anchor_type", "invalid_ref"}
    factory.assert_not_called()
    assert driver.queries == []


def test_empty_request_does_not_initialize_repository():
    from app.graph.models import GraphRetrievalRequest
    repo, _, factory = repository()
    assert repo.fetch_table_context(GraphRetrievalRequest("G", ())) == ()
    factory.assert_not_called()


def test_failed_driver_creation_can_retry_on_a_later_request_without_leaking_error(caplog):
    repo, driver, factory = repository()
    factory.side_effect = [ServiceUnavailable("unit-secret bolt://user:secret@internal"), driver]
    assert repo.fetch_table_context(request(ref()))[0].status == "unavailable"
    assert repo.fetch_table_context(request(ref()))[0].status == "success"
    assert factory.call_count == 2  # one attempt per caller request
    assert "unit-secret" not in caplog.text and "internal" not in caplog.text
    repo.close()
    assert driver.closes == 1


def test_close_failure_is_safe_and_not_repeated(caplog):
    repo, driver, _ = repository()
    repo.fetch_table_context(request(ref()))
    driver.close = Mock(side_effect=RuntimeError("unit-secret"))
    repo.close()
    repo.close()
    driver.close.assert_called_once()
    assert "unit-secret" not in caplog.text


def test_driver_error_while_consuming_rows_is_not_mistaken_for_success():
    repo, driver, _ = repository()
    def fail_during_iteration():
        yield row()
        raise TimeoutError("late server timeout")
    session = FakeSession(driver)
    session.run = lambda *args, **kwargs: fail_during_iteration()
    driver.session = lambda **kwargs: session
    assert repo.fetch_table_context(request(ref()))[0].status == "timeout"
    assert driver.closed_sessions == 1


def test_duplicate_rows_and_duplicate_entities_fail_closed():
    repo, _, _ = repository(FakeDriver(rows=[row(), row()]))
    assert repo.fetch_table_context(request(ref()))[0].status in {"ambiguous", "unavailable"}
    repo, _, _ = repository(FakeDriver(rows=[row(entities=[entity("E1"), entity("E1", name="different")])]))
    assert repo.fetch_table_context(request(ref()))[0].status == "unavailable"


def test_self_relationship_membership_does_not_reuse_same_structural_edge_in_one_match():
    from app.graph.templates import resolve_template
    query = resolve_template("table_context_v1")
    # Cypher's single-pattern relationship uniqueness would discard a self edge
    # if both CONTAINS_ENTITY hops used the same structural relationship.
    assert "EXISTS { MATCH (t)-[:CONTAINS_ENTITY]->(target) }" in query
    repo, _, _ = repository(FakeDriver(rows=[row(relationships=[edge(target="E1")])]))
    assert repo.fetch_table_context(request(ref()))[0].relationships[0].target_entity_id == "E1"


def test_shutdown_graph_close_survives_llm_cleanup_error(monkeypatch):
    import app.main as main
    from neo4j import GraphDatabase
    driver = FakeDriver()
    monkeypatch.setattr(GraphDatabase, "driver", Mock(return_value=driver))
    monkeypatch.setattr(main, "clear_llm_provider_cache", Mock(side_effect=RuntimeError("LLM cleanup error")))
    app = main.create_app(settings=settings())
    with pytest.raises(RuntimeError, match="LLM cleanup"):
        with TestClient(app):
            app.state.graph_repository.fetch_table_context(request(ref()))
    assert driver.closes == 1


def test_graph_and_executor_and_llm_lifecycles_coexist(monkeypatch):
    import app.main as main
    from neo4j import GraphDatabase
    driver = FakeDriver()
    executor = SimpleNamespace(start=Mock(), stop=Mock(), join=Mock())
    monkeypatch.setattr(GraphDatabase, "driver", Mock(return_value=driver))
    monkeypatch.setattr(main, "DocumentDeletionExecutor", Mock(return_value=executor))
    cleanup = Mock()
    monkeypatch.setattr(main, "clear_llm_provider_cache", cleanup)
    app = main.create_app(settings=settings(document_deletion_executor_enabled=True))
    with TestClient(app):
        app.state.graph_repository.fetch_table_context(request(ref()))
    executor.start.assert_called_once()
    executor.stop.assert_called_once()
    executor.join.assert_called_once_with(timeout=10)
    cleanup.assert_called_once()
    assert driver.closes == 1
