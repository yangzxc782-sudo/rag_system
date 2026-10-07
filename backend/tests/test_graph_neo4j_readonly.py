"""Opt-in, seedless acceptance of the ONLINE fixed read template on existing assets.

Only after explicit read authorization, set GRAPH_NEO4J_READONLY_TEST=anchor_context_v2
and GRAPH_NEO4J_READONLY_BINDINGS_JSON to ten SQL-verified binding records (ref,
source, provenance) in final-context discovery order. No source text or credentials
belong in that JSON. Connection credentials come only from existing NEO4J_* Settings.

No setup/cleanup writes, writer imports, SQL, LLM calls, or application startup.
The default test run skips before creating a driver. This is not RAG/LLM acceptance.
"""
import json
import os
import re

import pytest

from app.core.config import Settings
from app.extraction.kg_protocol import ANCHOR_ADAPTER
from app.graph.models import GraphSource, GraphTriggerProvenance, VerifiedAnchor
from app.graph.repository import Neo4jRepository
from app.graph.templates import resolve_template
from app.services.graph_retrieval import GraphRetrievalService


@pytest.mark.skipif(os.environ.get("GRAPH_NEO4J_READONLY_TEST") != "anchor_context_v2",
    reason="Existing-asset Neo4j READ acceptance requires explicit authorization and bindings")
def test_existing_graph_fixed_template_compiles_executes_and_decodes():
    text = resolve_template("anchor_context_v2")
    # Do not run if a future template revision introduces writes or procedures.
    assert not re.search(r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|LOAD|FOREACH)\b", text, re.I)
    assert not re.search(r"\bCALL\s+(?!\{)", text, re.I)
    config = Settings()
    assert config.graph_retrieval_enabled
    assert (config.graph_retrieval_max_anchors, config.graph_retrieval_max_entities_per_anchor,
        config.graph_retrieval_max_relationships_per_anchor, config.graph_retrieval_query_timeout_seconds,
        config.graph_retrieval_total_budget_seconds) == (8, 200, 400, 2, 5)
    raw = json.loads(os.environ["GRAPH_NEO4J_READONLY_BINDINGS_JSON"])
    assert len(raw) == 10
    anchors = tuple(VerifiedAnchor(ANCHOR_ADAPTER.validate_python(item["ref"]),
        GraphSource(**item["source"]), tuple(GraphTriggerProvenance(**p) for p in item["provenance"]))
        for item in raw)
    assert len({(a.ref.graph_id, a.ref.anchor_id) for a in anchors}) == 10
    assert all(a.fully_covered for a in anchors)

    class ObservedRepository(Neo4jRepository):
        batch_sizes = None
        decoded_records = 0

        def fetch_anchor_context(self, request):
            self.batch_sizes.append(len(request.anchors))
            return super().fetch_anchor_context(request)

        def _decode(self, binding, row, pack):
            self.decoded_records += 1
            return super()._decode(binding, row, pack)

    repo = ObservedRepository(config)
    repo.batch_sizes = []
    try:
        agent = repo._get_driver().get_server_info().agent
        result = GraphRetrievalService(config, repository=repo).retrieve(anchors)
        statuses = [item.status for item in result.items]
        assert statuses == ["success"] * 8 + ["limit_exceeded"] * 2
        assert repo.batch_sizes == [8]
        assert repo.decoded_records == 8
        print("Neo4j READ acceptance: " + json.dumps(dict(server=agent,
            query_compiled=True, query_executed=True, records=repo.decoded_records,
            batches=repo.batch_sizes, query_success=statuses.count("success"),
            limit_exceeded=statuses.count("limit_exceeded"),
            decoded_entities=sum(len(i.entities) for i in result.items),
            decoded_relationships=sum(len(i.relationships) for i in result.items))))
    finally:
        repo.close()
