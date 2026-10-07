"""Exact version-owned deletion targets. No migration/backfill or legacy KG query."""
from dataclasses import replace
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.models import DocumentProcessingJob, SourceDocumentVersion, GraphBuild
from app.models.document_chunk_set import ChunkSet
from app.search_engine.versioned_index import index_name
from app.services.document_processing import _now, _release, error


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GraphTarget(Target):
    graph_build_id: UUID
    source_version: UUID
    graph_id: str = Field(min_length=1, max_length=255)
    source_path: str
    payload_sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")


class IndexTarget(Target):
    chunk_set_id: UUID
    source_version: UUID
    graph_build_id: UUID
    index_name: str
    manifest_sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    embedding_fingerprint: str = Field(pattern="^[a-f0-9]{64}$")


class VersionTargets(Target):
    document_id: UUID
    source_versions: tuple[UUID, ...]
    job_ids: tuple[UUID, ...]
    graphs: tuple[GraphTarget, ...]
    indices: tuple[IndexTarget, ...]

    @model_validator(mode="after")
    def owned(self):
        builds = {g.graph_build_id: g for g in self.graphs}
        if (len(builds) != len(self.graphs) or len({g.graph_id for g in self.graphs}) != len(self.graphs)
                or len(set(self.source_versions)) != len(self.source_versions)
                or len(set(self.job_ids)) != len(self.job_ids)
                or len({s.chunk_set_id for s in self.indices}) != len(self.indices)):
            raise ValueError("Duplicate version deletion target")
        for graph in self.graphs:
            if graph.source_version not in self.source_versions or graph.source_path != (
                f"documents/{self.document_id}/sources/{graph.source_version}/graphs/{graph.graph_build_id}"):
                raise ValueError("Graph deletion owner mismatch")
        for item in self.indices:
            if (item.index_name != index_name(item.chunk_set_id) or item.graph_build_id not in builds
                    or builds[item.graph_build_id].source_version != item.source_version):
                raise ValueError("Index deletion owner mismatch")
        return self

    def prefixes(self):
        return tuple([f"kg-assets/{self.document_id}/{g.source_version}/{g.graph_build_id}/" for g in self.graphs]
            + [f"documents/{self.document_id}/chunk-sets/{s.chunk_set_id}/" for s in self.indices])


def version_schema_available(db):
    return isinstance(db, Session) and inspect(db.get_bind()).has_table(DocumentProcessingJob.__tablename__)


def check_processing_quiescent(db, document):
    if any((getattr(run, "source_metadata", None) or {}).get("requires_io_reconciliation")
           or (getattr(run, "source_metadata", None) or {}).get("external_write_pending")
           for run in (getattr(document, "parse_runs", None) or ())):
        raise error("DOCUMENT_DELETION_IO_RECONCILIATION_REQUIRED")
    if not version_schema_available(db):
        from app.services.versioned_document_guard import FROZEN_PROCESS_STATUSES
        if document.process_status in FROZEN_PROCESS_STATUSES:
            raise error("DOCUMENT_FROZEN_SOURCE_PROTECTED")
        return
    jobs = list(db.scalars(select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == document.id)
        .order_by(DocumentProcessingJob.id).with_for_update()))
    if any(j.status == "running" for j in jobs):
        raise error("DOCUMENT_PROCESSING_IN_PROGRESS")
    if any(j.checkpoint.get("requires_io_reconciliation") or j.checkpoint.get("external_write_pending") for j in jobs):
        raise error("DOCUMENT_DELETION_IO_RECONCILIATION_REQUIRED")


def prepare_version_deletion(db, document, manifest):
    """Caller holds Document lock. All changes join deletion admission commit."""
    if not version_schema_available(db):
        return manifest
    jobs = list(db.scalars(select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == document.id)
        .order_by(DocumentProcessingJob.id).with_for_update()))
    if any(j.status == "running" for j in jobs):
        raise error("DOCUMENT_PROCESSING_IN_PROGRESS")
    if any(j.checkpoint.get("requires_io_reconciliation") or j.checkpoint.get("external_write_pending") for j in jobs):
        raise error("DOCUMENT_DELETION_IO_RECONCILIATION_REQUIRED")
    sources = list(db.scalars(select(SourceDocumentVersion).where(SourceDocumentVersion.document_id == document.id)))
    graphs = list(db.scalars(select(GraphBuild).where(GraphBuild.document_id == document.id)))
    sets = list(db.scalars(select(ChunkSet).where(ChunkSet.document_id == document.id)))
    if not (jobs or sources or graphs or sets):
        return manifest
    targets = VersionTargets(document_id=document.id, source_versions=tuple(s.source_version for s in sources),
        job_ids=tuple(j.id for j in jobs), graphs=tuple(GraphTarget(graph_build_id=g.id,
            source_version=g.source_version, graph_id=g.graph_id, source_path=g.source_path,
            payload_sha256=g.write_checkpoint.get("payload_sha256")) for g in graphs),
        indices=tuple(IndexTarget(chunk_set_id=s.id, source_version=s.source_version, graph_build_id=s.graph_build_id,
            index_name=index_name(s.id), manifest_sha256=s.manifest_sha256,
            embedding_fingerprint=s.embedding_fingerprint) for s in sets))
    keys = list(manifest.derived_object_keys)
    for source in sources:
        if source.bucket_name != manifest.bucket_name or source.parse_run_id not in manifest.parse_run_ids:
            raise error("DOCUMENT_DELETION_SOURCE_CONFLICT")
        keys.extend((source.canonical_object_key, source.block_map_object_key))
    upgraded = replace(manifest, schema_version=2, versioned=targets.model_dump(mode="json"),
        derived_prefixes=manifest.derived_prefixes + targets.prefixes(), derived_object_keys=tuple(keys))
    for job in jobs:
        if job.status not in {"succeeded", "cancelled"}:
            _release(job, "cancelled", _now(db))
    if document.current_chunk_set_id:
        document.current_chunk_set_id = None
        document.publication_revision += 1
    return upgraded


def delete_versioned_indices(targets, *, client, checkpoint):
    for target in targets.indices:
        checkpoint()
        name = target.index_name
        if not client.indices.exists(index=name):
            continue
        mapping = client.indices.get_mapping(index=name)
        expected = dict(schema_version=2, document_id=str(targets.document_id),
            source_version=str(target.source_version), graph_build_id=str(target.graph_build_id),
            chunk_set_id=str(target.chunk_set_id), manifest_sha256=target.manifest_sha256,
            embedding_fingerprint=target.embedding_fingerprint)
        if set(mapping) != {name} or any(mapping[name].get("mappings", {}).get("_meta", {}).get(k) != v for k, v in expected.items()):
            raise error("DOCUMENT_DELETION_INDEX_OWNER_CONFLICT")
        # An alias or an aliased physical index is not a deletable owned target.
        aliases = client.indices.get_alias(index=name)
        if set(aliases) != {name} or aliases[name].get("aliases"):
            raise error("DOCUMENT_DELETION_INDEX_ALIAS_CONFLICT")
        checkpoint()
        result = client.indices.delete(index=name)
        if result.get("acknowledged") is not True or client.indices.exists(index=name):
            raise error("DOCUMENT_DELETION_INDEX_INCOMPLETE")


GRAPH_LOCK = """
MATCH (g:KnowledgeGraph {graph_id:$graph_id})
SET g._deletion_lock = true
RETURN properties(g) AS graph
"""
GRAPH_CHECK = """
MATCH (g:KnowledgeGraph {graph_id:$graph_id})
OPTIONAL MATCH (g)-[h]-(e)
RETURN count(h) AS total,
 count(CASE WHEN type(h)='HAS_ENTITY' AND startNode(h)=g AND e:MaterialEntity
 AND e.graph_id=$graph_id AND e.graph_build_id=$graph_build_id THEN 1 END) AS owned
"""
ENTITY_CHECK = """
MATCH (e:MaterialEntity {graph_id:$graph_id})
OPTIONAL MATCH (e)-[r]-(n)
RETURN count(DISTINCT e) AS entities,
 count(CASE WHEN e.graph_build_id IS NULL OR e.graph_build_id <> $graph_build_id
 OR NOT EXISTS { MATCH (:KnowledgeGraph {graph_id:$graph_id})-[:HAS_ENTITY]->(e) }
 OR (r IS NOT NULL AND NOT coalesce((
   (type(r)='HAS_ENTITY' AND startNode(r)=n AND n:KnowledgeGraph AND n.graph_id=$graph_id
      AND n.graph_build_id=$graph_build_id)
   OR (type(r)='RELATES_TO' AND n:MaterialEntity AND n.graph_id=$graph_id
      AND n.graph_build_id=$graph_build_id AND r.graph_id=$graph_id AND r.graph_build_id=$graph_build_id)), false))
 THEN 1 END) AS conflicts
"""
# Delete only validated owned edges. A concurrent foreign edge makes DELETE
# fail and roll back, rather than detaching an unverified relationship.
GRAPH_DELETE = """
MATCH (g:KnowledgeGraph {graph_id:$graph_id, graph_build_id:$graph_build_id})
OPTIONAL MATCH (g)-[h:HAS_ENTITY]->(e:MaterialEntity {graph_id:$graph_id, graph_build_id:$graph_build_id})
WITH g, collect(h) AS links, collect(e) AS entities
OPTIONAL MATCH (a:MaterialEntity {graph_id:$graph_id, graph_build_id:$graph_build_id})
 -[r:RELATES_TO {graph_id:$graph_id, graph_build_id:$graph_build_id}]->
 (b:MaterialEntity {graph_id:$graph_id, graph_build_id:$graph_build_id})
WITH g, links, entities, collect(r) AS relationships
FOREACH (r IN relationships | DELETE r)
FOREACH (h IN links | DELETE h)
FOREACH (e IN entities | DELETE e)
DELETE g
"""


def delete_versioned_graphs(targets, *, settings, checkpoint, driver=None):
    candidates = [g for g in targets.graphs if g.payload_sha256 is not None]
    # M2 persists this digest before invoking the writer. Unprepared builds
    # never wrote Neo4j and require only their owned object-prefix cleanup.
    if not candidates:
        return
    owned_driver = driver is None
    if owned_driver:
        if not settings.document_deletion_executor_enabled or not all((settings.kg_neo4j_uri, settings.kg_neo4j_database,
                settings.kg_neo4j_username, settings.kg_neo4j_password and settings.kg_neo4j_password.get_secret_value())):
            raise error("DOCUMENT_DELETION_GRAPH_CONFIG_UNAVAILABLE", 503)
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(settings.kg_neo4j_uri,
            auth=(settings.kg_neo4j_username, settings.kg_neo4j_password.get_secret_value()),
            connection_timeout=settings.neo4j_connection_timeout_seconds,
            connection_acquisition_timeout=settings.neo4j_connection_acquisition_timeout_seconds,
            max_transaction_retry_time=0)
    try:
        with driver.session(database=settings.kg_neo4j_database) as session:
            for target in candidates:
                checkpoint()
                parameters = target.model_dump(mode="json")
                with session.begin_transaction(timeout=settings.kg_write_timeout_seconds) as tx:
                    record = tx.run(GRAPH_LOCK, graph_id=target.graph_id).single()
                    if record is None:
                        tx.commit()
                        continue
                    graph = record["graph"]
                    owner = {**parameters, "document_id": str(targets.document_id), "graph_schema_version": 2}
                    if any(graph.get(k) != v for k, v in owner.items()) or target.payload_sha256 is None:
                        raise error("DOCUMENT_DELETION_GRAPH_OWNER_CONFLICT")
                    params = dict(graph_id=target.graph_id, graph_build_id=str(target.graph_build_id))
                    members = dict(tx.run(GRAPH_CHECK, **params).single(strict=True))
                    entities = dict(tx.run(ENTITY_CHECK, **params).single(strict=True))
                    if (members["total"] != members["owned"] or entities["conflicts"]
                            or members["owned"] != entities["entities"]
                            or entities["entities"] > 20000):
                        raise error("DOCUMENT_DELETION_GRAPH_EDGE_CONFLICT")
                    checkpoint()
                    tx.run(GRAPH_DELETE, graph_id=target.graph_id, graph_build_id=str(target.graph_build_id)).consume()
                    tx.commit()
    finally:
        if owned_driver:
            driver.close()
