"""Atomic, immutable graph writer. No DELETE, arbitrary Cypher or runtime DDL.

`built` is an external receipt, not retrieval admission: SQL GraphBuild must also
be sealed/ready and ownership verified. HAS_TRIPLE is a redundant derived path
in the package and intentionally is not written here.
"""
from __future__ import annotations

import json
from uuid import UUID

from app.extraction.kg_extract import relation_allowed, template
from app.extraction.kg_protocol import ANCHOR_ADAPTER
from app.ingestion.frozen_source import json_bytes, sha256_bytes

CONSTRAINTS = "SHOW CONSTRAINTS YIELD type, labelsOrTypes, properties RETURN type, labelsOrTypes, properties"
CLAIM = """
MERGE (g:KnowledgeGraph {graph_id: $graph_id})
ON CREATE SET g.source_path=$source_path, g.document_id=$document_id,
 g.source_version=$source_version, g.graph_build_id=$graph_build_id,
 g.graph_schema_version=2, g.payload_sha256=$payload_sha256,
 g.publication_status='staging', g.source_type='template_triples', g.mode='template_only',
 g.template=$template_version, g.entity_count=$entity_count, g.relationship_count=$relationship_count,
 g.unit_count=$unit_count
RETURN properties(g) AS graph
"""
ORPHANS = "MATCH (e:MaterialEntity {graph_id:$graph_id}) RETURN count(e) AS count"
ENTITIES = """
MATCH (g:KnowledgeGraph {graph_id:$graph_id, graph_build_id:$graph_build_id, publication_status:'staging'})
UNWIND $rows AS row
CREATE (e:MaterialEntity {graph_id:$graph_id, entity_id:row.id, entity_type:row.type,
 name:row.name, source_path:$source_path, heading:row.heading, tag:row.anchor_id,
 anchor_id:row.anchor_id, anchor_type:row.anchor_type, local_ref:row.local_ref,
 properties_json:row.properties_json, graph_build_id:$graph_build_id})
SET e.table_ref=row.table_ref
CREATE (g)-[:HAS_ENTITY]->(e)
RETURN count(e) AS count
"""
RELATIONSHIPS = """
UNWIND $rows AS row
MATCH (a:MaterialEntity {graph_id:$graph_id, graph_build_id:$graph_build_id, entity_id:row.source_id, anchor_id:row.anchor_id})
MATCH (b:MaterialEntity {graph_id:$graph_id, graph_build_id:$graph_build_id, entity_id:row.target_id, anchor_id:row.anchor_id})
CREATE (a)-[r:RELATES_TO {rel_id:row.id, graph_id:$graph_id, graph_build_id:$graph_build_id,
 relation_type:row.type, anchor_id:row.anchor_id, properties_json:row.properties_json}]->(b)
RETURN count(r) AS count
"""
COUNTS = """
MATCH (g:KnowledgeGraph {graph_id:$graph_id, graph_build_id:$graph_build_id})
OPTIONAL MATCH (g)-[:HAS_ENTITY]->(e:MaterialEntity)
WITH g, count(e) AS entities
OPTIONAL MATCH (g)-[:HAS_ENTITY]->(a:MaterialEntity)-[r:RELATES_TO]->(b:MaterialEntity)
RETURN entities, count(r) AS relationships,
 count(CASE WHEN r.graph_id=$graph_id AND r.graph_build_id=$graph_build_id
 AND a.graph_id=$graph_id AND b.graph_id=$graph_id
 AND a.graph_build_id=$graph_build_id AND b.graph_build_id=$graph_build_id
 AND r.anchor_id=a.anchor_id AND r.anchor_id=b.anchor_id THEN 1 END) AS owned_relationships
"""
SEAL = """
MATCH (g:KnowledgeGraph {graph_id:$graph_id, graph_build_id:$graph_build_id, payload_sha256:$payload_sha256})
SET g.publication_status='built'
RETURN count(g) AS count
"""


def validate_payload(payload: dict, *, max_entities: int = 20000, max_relationships: int = 40000) -> None:
    for key in ("document_id", "source_version", "graph_build_id"):
        UUID(payload[key])
    if payload["graph_schema_version"] != 2 or not payload["graph_id"] or not payload["source_path"]:
        raise ValueError("Invalid graph identity")
    if len(payload["entities"]) > max_entities or len(payload["relationships"]) > max_relationships:
        raise ValueError("Graph write budget exceeded")
    if len(json_bytes(payload)) > 32_000_000:
        raise ValueError("Graph byte budget exceeded")
    pack, anchors, ids = template(), {}, {}
    last_end = 0
    for unit in payload["units"]:
        anchor = ANCHOR_ADAPTER.validate_python(unit["anchor"])
        start, end = unit["source_start"], unit["source_end"]
        if (anchor.graph_id != payload["graph_id"] or anchor.anchor_id in anchors
                or type(start) is not int or type(end) is not int
                or not last_end <= start < end <= payload["character_count"]):
            raise ValueError("Invalid anchor owner/range")
        last_end = end
        if type(unit["eligible"]) is not bool:
            raise ValueError("Invalid unit publication flag")
        anchors[anchor.anchor_id] = unit
    for entity in payload["entities"]:
        props = entity["properties"]
        unit = anchors.get(props.get("anchor_id"))
        if (not unit or not unit["eligible"] or props.get("graph_id") != payload["graph_id"]
                or entity["type"] not in pack["entity_type_whitelist"]
                or not entity["id"].startswith(props["anchor_id"] + "::")
                or entity["id"] in ids or not entity["name"].strip()
                or props.get("anchor_type") != unit["anchor"]["anchor_type"]):
            raise ValueError("Invalid entity identity/owner")
        ids[entity["id"]] = entity
    seen, qualifying = set(), set()
    for rel in payload["relationships"]:
        a, b = ids.get(rel["source_id"]), ids.get(rel["target_id"])
        props = rel["properties"]
        if (not a or not b or rel["id"] in seen or props.get("graph_id") != payload["graph_id"]
                or props.get("anchor_id") != a["properties"]["anchor_id"]
                or props.get("anchor_id") != b["properties"]["anchor_id"]
                or not relation_allowed(rel["type"], a["type"], b["type"], pack)):
            raise ValueError("Invalid relationship endpoint/type/owner")
        seen.add(rel["id"])
        qualifying.add(props["anchor_id"])
    if qualifying != {key for key, u in anchors.items() if u["eligible"]}:
        raise ValueError("Anchor eligibility does not match qualified triples")


class AtomicGraphWriter:
    def __init__(self, driver, *, database: str, timeout_seconds: float = 30):
        self.driver, self.database, self.timeout = driver, database, timeout_seconds

    def write(self, payload: dict) -> dict:
        validate_payload(payload)
        digest = sha256_bytes(json_bytes(payload))
        expected = {key: payload[key] for key in ("graph_id", "source_path", "document_id", "source_version", "graph_build_id")}
        expected.update(payload_sha256=digest, graph_schema_version=2,
                        template_version=payload["template_version"], entity_count=len(payload["entities"]),
                        relationship_count=len(payload["relationships"]), unit_count=len(payload["units"]))
        with self.driver.session(database=self.database) as session:
            # A missing constraint is a deployment error, never a warning to skip.
            from neo4j import Query
            available = {(tuple(row["labelsOrTypes"]), tuple(row["properties"]))
                         for row in session.run(Query(CONSTRAINTS, timeout=self.timeout))
                         if row["type"] in {"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS"}}
            required = {(("KnowledgeGraph",), ("graph_id",)), (("KnowledgeGraph",), ("source_path",)),
                        (("MaterialEntity",), ("graph_id", "entity_id"))}
            if not required.issubset(available):
                raise ValueError("Required KG uniqueness constraints unavailable")
            with session.begin_transaction(timeout=self.timeout) as tx:
                graph = tx.run(CLAIM, **expected).single(strict=True)["graph"]
                for key, value in expected.items():
                    stored_key = "template" if key == "template_version" else key
                    if graph.get(stored_key) != value:
                        raise ValueError("Existing graph identity or payload conflict")
                if graph.get("publication_status") not in {"staging", "built"}:
                    raise ValueError("Unsupported graph state")
                if graph["publication_status"] == "staging":
                    if tx.run(ORPHANS, **expected).single(strict=True)["count"]:
                        raise ValueError("Existing staged entities conflict")
                    entity_rows = []
                    for row in payload["entities"]:
                        props = row["properties"]
                        entity_rows.append(dict(id=row["id"], type=row["type"], name=row["name"],
                            heading=props.get("标题层级", ""), anchor_id=props["anchor_id"],
                            local_ref=props["local_ref"], anchor_type=props["anchor_type"],
                            table_ref=props.get("table_ref"), properties_json=json.dumps(props, ensure_ascii=False)))
                    relationship_rows = [dict(id=r["id"], source_id=r["source_id"], target_id=r["target_id"],
                        type=r["type"], anchor_id=r["properties"]["anchor_id"],
                        properties_json=json.dumps(r["properties"], ensure_ascii=False)) for r in payload["relationships"]]
                    for query, rows in ((ENTITIES, entity_rows), (RELATIONSHIPS, relationship_rows)):
                        for start in range(0, len(rows), 200):
                            batch = rows[start:start + 200]
                            if tx.run(query, rows=batch, **expected).single(strict=True)["count"] != len(batch):
                                raise ValueError("Graph write cardinality mismatch")
                counts = dict(tx.run(COUNTS, **expected).single(strict=True))
                if counts != dict(entities=expected["entity_count"], relationships=expected["relationship_count"],
                                  owned_relationships=expected["relationship_count"]):
                    raise ValueError("Graph verification failed")
                if graph["publication_status"] == "staging" and tx.run(SEAL, **expected).single(strict=True)["count"] != 1:
                    raise ValueError("Graph receipt seal failed")
                tx.commit()
        return dict(payload_sha256=digest, entities=expected["entity_count"], relationships=expected["relationship_count"],
                    graph_build_id=payload["graph_build_id"], status="built")


def configured_writer(settings) -> AtomicGraphWriter:
    if not settings.kg_build_enabled or not all((settings.kg_neo4j_uri, settings.kg_neo4j_database,
            settings.kg_neo4j_username, settings.kg_neo4j_password and settings.kg_neo4j_password.get_secret_value())):
        raise ValueError("KG writer configuration unavailable")
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(settings.kg_neo4j_uri,
        auth=(settings.kg_neo4j_username, settings.kg_neo4j_password.get_secret_value()),
        connection_timeout=settings.neo4j_connection_timeout_seconds,
        connection_acquisition_timeout=settings.neo4j_connection_acquisition_timeout_seconds,
        max_transaction_retry_time=0)
    return AtomicGraphWriter(driver, database=settings.kg_neo4j_database,
                             timeout_seconds=settings.kg_write_timeout_seconds)
