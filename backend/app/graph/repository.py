from __future__ import annotations
from collections.abc import Callable
import json
import logging
import math
import re
from threading import Lock
import time
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.extraction.kg_extract import relation_allowed, template
from app.graph.models import GraphAnchorResult, GraphEntity, GraphRelationship, GraphRetrievalRequest
from app.graph.templates import resolve_template


logger = logging.getLogger(__name__)


def _error_code(exc):
    try:
        code = getattr(exc, "code", None)
    except Exception:
        return None
    return code if (type(code) is str and len(code) <= 160 and re.fullmatch(
        r"Neo\.(?:ClientError|TransientError|DatabaseError)\.[A-Za-z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9]*", code)) else None


def _decode_reason(exc):
    # Match only our constant messages; never stringify exceptions or log values.
    message = exc.args[0] if exc.args and type(exc.args[0]) is str else None
    return {
        "Graph ownership mismatch": "binding_mismatch",
        "Cross-graph/anchor data": "binding_mismatch",
        "Graph property ownership mismatch": "binding_mismatch",
        "Entity property identity mismatch": "binding_mismatch",
        "Entity identity/type mismatch": "entity_identity_mismatch",
        "Relationship endpoint/type mismatch": "relationship_endpoint_mismatch",
    }.get(message, "neo4j_decode_error")


def _log_failure(exc, *, request, config, batch_id, started, phase, status, reason, anchor_index=None):
    elapsed = max(0, (time.monotonic() - started) * 1000)
    remaining = request.remaining_budget_ms
    remaining = (max(0, remaining - elapsed)
        if type(remaining) in (int, float) and math.isfinite(remaining) else None)
    name = type(exc).__name__
    diagnostics = dict(template_name="anchor_context_v2", phase=phase, batch_id=batch_id,
        anchor_count=len(request.anchors), max_anchor_count=config.graph_retrieval_max_anchors,
        exception_type=name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "Exception",
        neo4j_code=_error_code(exc), elapsed_ms=round(elapsed, 3),
        remaining_budget_ms=round(remaining, 3) if remaining is not None else None,
        query_status=status, reason=reason, anchor_index=anchor_index)
    logger.warning("Graph retrieval failed (%s): %s", status,
        json.dumps(diagnostics, sort_keys=True, allow_nan=False),
        extra={"event": "graph_retrieval_failure", "graph_diagnostics": diagnostics})


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

    def fetch_anchor_context(self, request: GraphRetrievalRequest) -> tuple[GraphAnchorResult, ...]:
        config = self._settings
        if not config.graph_retrieval_enabled:
            return tuple(GraphAnchorResult(a, "disabled") for a in request.anchors)
        if len(request.anchors) > config.graph_retrieval_max_anchors:
            return tuple(GraphAnchorResult(a, "limit_exceeded") for a in request.anchors)
        started, batch_id, phase = time.monotonic(), uuid4().hex, "connect"
        first = request.anchors[0]
        source = first.source
        pack = template()
        params = dict(graph_id=first.ref.graph_id, document_id=source.document_id,
            source_version=source.source_version, graph_build_id=source.graph_build_id,
            source_path=source.source_path, payload_sha256=source.payload_sha256,
            template_version=source.template_version,
            anchors=[dict(anchor_id=a.ref.anchor_id, anchor_type=a.ref.anchor_type) for a in request.anchors],
            entity_types=pack["entity_type_whitelist"], relation_specs=pack["relation_type_whitelist"],
            entity_fetch_limit=config.graph_retrieval_max_entities_per_anchor + 1,
            relationship_fetch_limit=config.graph_retrieval_max_relationships_per_anchor + 1)
        try:
            from neo4j import Query, READ_ACCESS
            with self._get_driver().session(database=config.neo4j_database, default_access_mode=READ_ACCESS,
                    disable_auto_commit_retries=True) as session:
                phase = "query"
                cursor = session.run(Query(resolve_template("anchor_context_v2"),
                    timeout=config.graph_retrieval_query_timeout_seconds), parameters=params)
                phase = "read_result"
                rows = []
                for record in cursor:
                    rows.append(dict(record))
                    if len(rows) > len(request.anchors):
                        raise ValueError("Unexpected graph result cardinality")
            phase = "validate_result"
            grouped = {}
            for row in rows:
                if row["anchor_id"] not in {a.ref.anchor_id for a in request.anchors}:
                    raise ValueError("Unexpected graph anchor")
                grouped.setdefault(row["anchor_id"], []).append(row)
        except Exception as exc:
            status = "timeout" if _is_timeout(exc) else "unavailable"
            _log_failure(exc, request=request, config=config, batch_id=batch_id, started=started,
                phase=phase, status=status, reason="neo4j_query_error")
            return tuple(GraphAnchorResult(a, status) for a in request.anchors)
        result = []
        for anchor_index, a in enumerate(request.anchors):
            matches = grouped.get(a.ref.anchor_id, [])
            try:
                item = (self._decode(a, matches[0], pack) if len(matches) == 1 else
                        GraphAnchorResult(a, "ambiguous" if matches else "not_found"))
            except (ValueError, KeyError, TypeError, RecursionError) as exc:
                _log_failure(exc, request=request, config=config, batch_id=batch_id, started=started,
                    phase="decode", status="unavailable", reason=_decode_reason(exc), anchor_index=anchor_index)
                item = GraphAnchorResult(a, "unavailable")
            result.append(item)
        return tuple(result)

    def _decode(self, binding, row, pack):
        ref, source = binding.ref, binding.source
        count = row["candidate_count"]
        if type(count) is not int or count not in (0, 1, 2):
            raise ValueError("Invalid graph count")
        if count != 1:
            return GraphAnchorResult(binding, "ambiguous" if count else "not_found")
        graph = row["graph"]
        expected = dict(graph_id=ref.graph_id, graph_build_id=source.graph_build_id, document_id=source.document_id,
            source_version=source.source_version, payload_sha256=source.payload_sha256, source_path=source.source_path,
            template=source.template_version, graph_schema_version=2, publication_status="built")
        if any(graph.get(k) != v for k, v in expected.items()):
            raise ValueError("Graph ownership mismatch")
        raw_entities, raw_edges = _maps(row, "entities"), _maps(row, "relationships")
        if (len(raw_entities) > self._settings.graph_retrieval_max_entities_per_anchor
                or len(raw_edges) > self._settings.graph_retrieval_max_relationships_per_anchor):
            return GraphAnchorResult(binding, "truncated")
        nodes, edges = {}, {}
        for value in raw_entities:
            _owned(value, binding)
            ident, kind = _string(value, "entity_id"), _string(value, "entity_type")
            if (value.get("anchor_type") != ref.anchor_type or kind not in pack["entity_type_whitelist"]
                    or not ident.startswith(ref.anchor_id + "::")):
                raise ValueError("Entity identity/type mismatch")
            props = _properties(value, binding)
            if props.get("anchor_type") != ref.anchor_type:
                raise ValueError("Entity property identity mismatch")
            node = GraphEntity(ident, _string(value, "name"), kind, _business_properties(props))
            if ident in nodes:
                raise ValueError("Duplicate entity identity")
            nodes[ident] = node
        for value in raw_edges:
            _owned(value, binding)
            ident, kind = _string(value, "rel_id"), _string(value, "relation_type")
            left, right = _string(value, "source_entity_id"), _string(value, "target_entity_id")
            if (left not in nodes or right not in nodes or ident != f"{left}::{kind}::{right}"
                    or not relation_allowed(kind, nodes[left].entity_type, nodes[right].entity_type, pack)):
                raise ValueError("Relationship endpoint/type mismatch")
            edge = GraphRelationship(ident, kind, left, right, _business_properties(_properties(value, binding)))
            if ident in edges:
                raise ValueError("Duplicate relationship identity")
            edges[ident] = edge
        if not nodes or not edges:
            return GraphAnchorResult(binding, "not_found")
        return GraphAnchorResult(binding, "success", tuple(nodes[k] for k in sorted(nodes)),
                                 tuple(edges[k] for k in sorted(edges)))


def _owned(value, binding):
    if any(value.get(k) != v for k, v in dict(graph_id=binding.ref.graph_id,
            graph_build_id=binding.source.graph_build_id, anchor_id=binding.ref.anchor_id).items()):
        raise ValueError("Cross-graph/anchor data")


def _properties(value, binding):
    raw = _string(value, "properties_json", limit=65536)
    if len(raw.encode("utf-8")) > 65536:
        raise ValueError("Graph property budget exceeded")
    def pairs(items):
        obj = {}
        for k, v in items:
            if k in obj:
                raise ValueError("Duplicate property")
            obj[k] = v
        return obj
    def invalid_constant(value):
        raise ValueError("Non-finite graph property")
    props = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    json.dumps(props, allow_nan=False)  # Also rejects overflowing exponents such as 1e999.
    if (not isinstance(props, dict) or props.get("graph_id") != binding.ref.graph_id
            or props.get("anchor_id") != binding.ref.anchor_id):
        raise ValueError("Graph property ownership mismatch")
    return props


def _business_properties(props):
    # Preserve whole value/condition/provenance objects. Only control identity is projected away.
    return {k: v for k, v in props.items() if k not in
            {"graph_id", "anchor_id", "anchor_type", "local_ref", "table_ref", "知识编码"}}


def _string(value, key, *, limit=2048):
    item = value[key]
    if not isinstance(item, str) or not item.strip() or len(item) > limit:
        raise ValueError("Invalid graph scalar")
    return item


def _maps(row, key):
    items = row[key]
    if not isinstance(items, list) or any(type(item) is not dict for item in items):
        raise ValueError("Graph result must contain plain maps")
    return items


def _is_timeout(exc):
    return isinstance(exc, TimeoutError) or _error_code(exc) in {
        "Neo.ClientError.Transaction.TransactionTimedOut",
        "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
        "Neo.TransientError.Transaction.TransactionTimedOut",
    }
