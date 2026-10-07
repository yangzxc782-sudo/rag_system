"""Synthetic v2 fixtures. FixtureAuthority is a test double; SQL authority is tested separately."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

from app.core.config import Settings
from app.extraction.kg_extract import qualify
from app.extraction.kg_protocol import ANCHOR_ADAPTER
from app.graph.models import GraphSource, GraphTriggerProvenance, VerifiedAnchor, GraphRetrievalRequest
from app.graph.repository import Neo4jRepository
from app.ingestion.frozen_source import sha256_bytes
from test_kg_v2_protocol_units import raw_part


def settings(**changes):
    values = dict(graph_retrieval_enabled=True, pdf_kg_search_enabled=True,
        neo4j_uri="bolt://graph.example.invalid:7687", neo4j_database="neo4j",
        neo4j_username="graph_reader", neo4j_password="unit-secret", document_deletion_executor_enabled=False,
        llm_provider="local", llm_model="test", llm_base_url="http://localhost:11434/v1")
    values.update(changes)
    return Settings(_env_file=None, **values)


def nested_properties():
    """Synthetic JSON values, including nested provenance and objects inside arrays."""
    return {"z": "中文 Ω 😀", "a": 'quote" slash\\ newline\n', "number": 1,
        "float": 1.0, "bool": True, "null": None, "object": {}, "array": [],
        "provenance": {"unit": "MPa", "condition": {"z": "hot", "a": "cast"}},
        "items": [{"z": 2, "a": 1}, {"b": False, "a": None}],
        "nested": [[{"z": 3.5, "a": []}]]}


def reorder_objects(value):
    """Offline persistence fixture: reorder objects only, not a real JSONB round trip."""
    if isinstance(value, dict):
        return {key: reorder_objects(value[key]) for key in reversed(value)}
    if isinstance(value, list):
        return [reorder_objects(item) for item in value]
    return value


def ref(n=1, *, kind="table", graph="G"):
    local = ("T-" if kind == "table" else "C-") + str(n)
    return ANCHOR_ADAPTER.validate_python(dict(anchor_id=f"{graph}::{local}", graph_id=graph,
        anchor_type=kind, **({"table_ref": local, "table_no": str(n)} if kind == "table" else {"clause_ref": local})))


def binding(n=1, *, kind="table", graph="G", start=0, end=20):
    r = ref(n, kind=kind, graph=graph)
    source = GraphSource(str(UUID(int=101)), str(UUID(int=201)), str(UUID(int=301)),
        str(UUID(int=500+n)), start, end, "a"*64, "b"*64, "kg/source", "4.0.0")
    origin = GraphTriggerProvenance(r.anchor_id, source.document_id, str(UUID(int=n)), n,
        source.source_version, source.graph_build_id, str(UUID(int=401)), start, end)
    return VerifiedAnchor(r, source, (origin,))


def row(a=None, **changes):
    a = a or binding()
    s, r = a.source, a.ref
    part = qualify(raw_part(), r.business_metadata(), 0)
    entities = [dict(entity_id=e["id"], name=e["name"], entity_type=e["type"],
        graph_id=r.graph_id, graph_build_id=s.graph_build_id, anchor_id=r.anchor_id, anchor_type=r.anchor_type,
        properties_json=json.dumps(e["properties"], ensure_ascii=False)) for e in part["entities"]]
    rels = [dict(rel_id=e["id"], relation_type=e["type"], source_entity_id=e["source_id"],
        target_entity_id=e["target_id"], graph_id=r.graph_id, graph_build_id=s.graph_build_id,
        anchor_id=r.anchor_id, properties_json=json.dumps(e["properties"], ensure_ascii=False)) for e in part["relationships"]]
    result = dict(anchor_id=r.anchor_id, candidate_count=1, graph=dict(graph_id=r.graph_id,
        graph_build_id=s.graph_build_id, document_id=s.document_id, source_version=s.source_version,
        source_path=s.source_path, payload_sha256=s.payload_sha256, graph_schema_version=2,
        publication_status="built", template=s.template_version), entities=entities, relationships=rels)
    result.update(changes)
    return result


class FakeSession:
    def __init__(self, driver): self.driver = driver
    def __enter__(self): return self
    def __exit__(self, *args): self.driver.closed_sessions += 1
    def run(self, query, parameters):
        self.driver.queries.append((query, deepcopy(parameters)))
        if self.driver.error: raise self.driver.error
        return iter(deepcopy(self.driver.rows))


class FakeDriver:
    def __init__(self, rows=None, error=None):
        self.rows, self.error = rows if rows is not None else [row()], error
        self.queries, self.sessions = [], []
        self.closed_sessions = self.closes = 0
    def session(self, **kwargs):
        self.sessions.append(kwargs)
        return FakeSession(self)
    def close(self): self.closes += 1


def repository(driver=None, **overrides):
    driver = driver or FakeDriver()
    factory = Mock(return_value=driver)
    return Neo4jRepository(settings(**overrides), driver_factory=factory), driver, factory


def item(n=1, *, refs=None, metadata=None, content="文本说明 ZL101 ≥350 MPa。", score=None, kind="table"):
    return SimpleNamespace(chunk_id=str(UUID(int=n)), document_id=str(UUID(int=100+n)), original_filename="source.pdf",
        chunk_index=n, content=content, hybrid_score=score if score is not None else 1/n,
        source_metadata=metadata if metadata is not None else {"kg_refs": refs if refs is not None else [ref(n, kind=kind, graph=f"G{n}").business_metadata()]},
        retrieval_source="both", keyword_score=1.0, vector_score=1.0, keyword_rank=n, vector_rank=n,
        matched_keywords=[], embedding_model="fake", embedding_dim=1024,
        source_version=str(UUID(int=200+n)), graph_build_id=str(UUID(int=300+n)), chunk_set_id=str(UUID(int=400+n)),
        source_start=0, source_end=len(content), content_sha256=sha256_bytes(content.encode()), embedding_fingerprint="c"*64)


def with_content(item, content):
    return replace(item, content=content, source_end=item.source_start+len(content), content_sha256=sha256_bytes(content.encode()))


class FixtureAuthority:
    def resolve(self, context, *, current=True):
        result = []
        for c in context.chunks:
            if c.chunk_set_id is None:
                raise ValueError("Legacy fixture")
            for metadata in (c.source_metadata or {}).get("kg_refs", []):
                r = ANCHOR_ADAPTER.validate_python(metadata)
                if c.effective_start == c.effective_end:
                    continue
                source = GraphSource(c.document_id, c.source_version, c.graph_build_id, str(UUID(int=500+c.chunk_index)),
                    c.source_start, c.source_end, "a"*64, "b"*64, f"kg/{c.graph_build_id}", "4.0.0")
                origin = GraphTriggerProvenance(r.anchor_id, c.document_id, c.chunk_id, c.citation_id,
                    c.source_version, c.graph_build_id, c.chunk_set_id, c.effective_start, c.effective_end)
                result.append(VerifiedAnchor(r, source, (origin,)))
        return tuple(result)
