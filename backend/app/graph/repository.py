from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import logging
from threading import Lock
from typing import Any

from app.core.config import Settings
from app.graph.models import (
    GraphAnchorResult, GraphDocument, GraphEntity, GraphRelationship,
    GraphRetrievalRequest, GraphTable, GraphTruncation, KGRef, unsupported_ref_status,
)
from app.graph.templates import RELATIONSHIP_TYPES, resolve_template


logger = logging.getLogger(__name__)


class Neo4jRepository:
    """A lazy reusable driver, short READ sessions, and one fixed template.

    The owner must supply an account with no database write privileges. No
    importer dependency, arbitrary Cypher method, automatic retry, or startup IO.
    Shutdown calls close after in-flight requests have drained.
    """

    def __init__(self, settings: Settings, *, driver_factory: Callable[..., Any] | None = None):
        self._settings = settings
        self._driver_factory = driver_factory
        self._driver = None
        self._closed = False
        self._lock = Lock()

    def _get_driver(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Graph repository closed")
            if self._driver is None:
                config = self._settings
                if (not all(isinstance(value, str) and value.strip() for value in
                            (config.neo4j_uri, config.neo4j_database, config.neo4j_username))
                        or config.neo4j_password is None or not config.neo4j_password.get_secret_value()):
                    raise RuntimeError("Graph configuration unavailable")
                from neo4j import GraphDatabase
                factory = self._driver_factory or GraphDatabase.driver
                self._driver = factory(
                    config.neo4j_uri,
                    auth=(config.neo4j_username, config.neo4j_password.get_secret_value()),
                    connection_timeout=config.neo4j_connection_timeout_seconds,
                    connection_acquisition_timeout=config.neo4j_connection_acquisition_timeout_seconds,
                    max_transaction_retry_time=0,
                )
            return self._driver

    def close(self) -> None:
        with self._lock:
            driver, self._driver = self._driver, None
            self._closed = True
        if driver is not None:
            try:
                driver.close()
            except Exception:
                logger.warning("Graph driver close failed.")

    def fetch_table_context(self, request: GraphRetrievalRequest) -> tuple[GraphAnchorResult, ...]:
        config = self._settings
        if not config.graph_retrieval_enabled:
            return tuple(GraphAnchorResult(ref=ref, status="disabled") for ref in request.anchors)
        prepared: dict[str, GraphAnchorResult] = {}
        anchors = []
        for ref in request.anchors:
            status = unsupported_ref_status(ref)
            if status is None and len(anchors) >= config.graph_retrieval_max_anchors:
                status = "limit_exceeded"
            if status is None:
                anchors.append(ref)
            else:
                prepared[ref.anchor_id] = GraphAnchorResult(ref=ref, status=status)
        if anchors:
            prepared.update(self._fetch(request.graph_id, anchors))
        return tuple(replace(prepared[ref.anchor_id], provenance=tuple(
            item for item in request.provenance if item.anchor_id == ref.anchor_id
        )) for ref in request.anchors)

    def _fetch(self, graph_id: str, anchors: list[KGRef]) -> dict[str, GraphAnchorResult]:
        config = self._settings
        try:
            from neo4j import Query, READ_ACCESS
            driver = self._get_driver()
            with driver.session(database=config.neo4j_database, default_access_mode=READ_ACCESS,
                                disable_auto_commit_retries=True) as session:
                cursor = session.run(
                    Query(resolve_template("table_context_v1"),
                          timeout=config.graph_retrieval_query_timeout_seconds),
                    parameters={
                        "graph_id": graph_id,
                        "anchors": [{"anchor_id": ref.anchor_id, "table_ref": ref.table_ref} for ref in anchors],
                        "max_entities": config.graph_retrieval_max_entities_per_anchor,
                        "entity_fetch_limit": config.graph_retrieval_max_entities_per_anchor + 1,
                        "relationship_fetch_limit": config.graph_retrieval_max_relationships_per_anchor + 1,
                    },
                )
                rows = []
                for record in cursor:
                    rows.append(dict(record))
                    if len(rows) > len(anchors):
                        raise ValueError("Unexpected graph result cardinality")
            grouped: dict[str, list[dict]] = {}
            for row in rows:
                grouped.setdefault(row["anchor_id"], []).append(row)
        except Exception as exc:
            status = "timeout" if _is_timeout(exc) else "unavailable"
            logger.warning("Graph retrieval timed out." if status == "timeout" else "Graph retrieval unavailable.")
            return {ref.anchor_id: GraphAnchorResult(ref=ref, status=status) for ref in anchors}

        result = {}
        for ref in anchors:
            matches = grouped.get(ref.anchor_id, [])
            try:
                if not matches:
                    item = GraphAnchorResult(ref=ref, status="not_found")
                elif len(matches) != 1:
                    item = GraphAnchorResult(ref=ref, status="ambiguous")
                else:
                    item = self._decode(ref, matches[0])
            except (ValueError, TypeError, KeyError):
                item = GraphAnchorResult(ref=ref, status="unavailable")
            result[ref.anchor_id] = replace(item, template_name="table_context_v1")
        return result

    def _decode(self, ref: KGRef, row: dict) -> GraphAnchorResult:
        if row["graph_id"] != ref.graph_id:
            raise ValueError("Graph identity mismatch")
        count = row["candidate_count"]
        if type(count) is not int or count not in {0, 1, 2}:
            raise ValueError("Invalid candidate count")
        if count == 0:
            return GraphAnchorResult(ref=ref, status="not_found")
        if count == 2 or row["shared_document"] is True:
            return GraphAnchorResult(ref=ref, status="ambiguous")
        if row["shared_document"] is not False:
            raise ValueError("Shared document isolation not proven")
        raw_doc, raw_table = row["document"], row["table"]
        document = GraphDocument(_string(raw_doc, "doc_id"), _optional_string(raw_doc, "source_pdf"),
                                 _optional_string(raw_doc, "source_type"))
        table = GraphTable(_string(raw_table, "table_id"), _string(raw_table, "table_ref"),
                           _integer(raw_table, "page"), _integer(raw_table, "table_index"))
        if table.table_ref != ref.table_ref or table.table_id != f"{document.doc_id}_{table.table_ref}":
            raise ValueError("Table identity mismatch")
        nodes: dict[str, GraphEntity] = {}
        for value in _maps(row, "entities"):
            if value.get("doc_id") != document.doc_id or value.get("table_ref") != table.table_ref:
                continue
            node = GraphEntity(_string(value, "id"), _optional_string(value, "name"),
                               _optional_string(value, "entity_type"), document.doc_id,
                               table.table_ref, _integer(value, "page"))
            if node.id in nodes and node != nodes[node.id]:
                raise ValueError("Conflicting entity identity")
            nodes[node.id] = node
        entity_limit = self._settings.graph_retrieval_max_entities_per_anchor
        entities_truncated = len(nodes) > entity_limit
        entities = tuple(sorted(nodes.values(), key=lambda node: node.id)[:entity_limit])
        selected_ids = {node.id for node in entities}
        edges = set()
        for value in _maps(row, "relationships"):
            if (value.get("graph_id") != ref.graph_id or value.get("doc_id") != document.doc_id
                    or value.get("type") not in RELATIONSHIP_TYPES
                    or value.get("source_entity_id") not in selected_ids
                    or value.get("target_entity_id") not in selected_ids):
                continue
            edges.add(GraphRelationship(*(_string(value, key) for key in
                ("type", "source_entity_id", "target_entity_id", "graph_id", "doc_id"))))
        relationship_limit = self._settings.graph_retrieval_max_relationships_per_anchor
        relationships_truncated = len(edges) > relationship_limit
        relationships = tuple(sorted(edges, key=lambda edge:
            (edge.source_entity_id, edge.type, edge.target_entity_id))[:relationship_limit])
        return GraphAnchorResult(
            ref=ref, status="truncated" if entities_truncated or relationships_truncated else "success",
            document=document, table=table, entities=entities, relationships=relationships,
            truncation=GraphTruncation(entities_truncated, relationships_truncated, entities_truncated),
        )


def _string(value: dict, key: str) -> str:
    item = value[key]
    if not isinstance(item, str) or not item.strip():
        raise ValueError("Invalid graph scalar")
    return item


def _optional_string(value: dict, key: str) -> str | None:
    item = value.get(key)
    if item is not None and not isinstance(item, str):
        raise ValueError("Invalid graph scalar")
    return item


def _integer(value: dict, key: str) -> int | None:
    item = value.get(key)
    if item is not None and type(item) is not int:
        raise ValueError("Invalid graph scalar")
    return item


def _maps(row: dict, key: str) -> list[dict]:
    items = row[key]
    if not isinstance(items, list) or any(type(item) is not dict for item in items):
        raise ValueError("Graph result must contain plain maps")
    return items


def _is_timeout(exc: Exception) -> bool:
    # Never inspect or forward server error messages, queries or credentials.
    return isinstance(exc, TimeoutError) or getattr(exc, "code", "") in {
        "Neo.ClientError.Transaction.TransactionTimedOut",
        "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
        "Neo.TransientError.Transaction.TransactionTimedOut",
    }
