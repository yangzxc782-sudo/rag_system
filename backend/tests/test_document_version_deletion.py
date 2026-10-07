"""M5 exact deletion contracts, synthetic gateways + SQLite dependency checks."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from app.core.errors import BusinessError
from app.models import Document, DocumentProcessingJob, DocumentChunk, DocumentParseRun, SourceDocumentVersion, GraphBuild, KGExtractionUnit
from app.models.document_chunk_set import ChunkSet
from app.models.document_deletion_job import DocumentDeletionJob
from app.services import document_deletion as deletion
from app.services import document_version_deletion as versions
from app.services.document_deletion_manifest import DocumentDeletionManifest
from app.services.document_graph_builds import _now
from test_document_processing import db, settings, parsed, finish
from test_kg_v2_builds import Provider, Writer
from test_chunk_set_builds import Encoder, Index


def published(db, settings, monkeypatch):
    doc, job, assets, _ = parsed(db, settings, monkeypatch)
    state, _ = finish(db, settings, doc, job, assets, graph_provider=Provider(db), writer=Writer(db),
        encoder=Encoder(db, settings), index=Index(db))
    settings.document_deletion_executor_enabled = True
    return doc, state, assets


def test_version_manifest_revokes_publication_and_finalizes_dependencies(db, settings, monkeypatch):
    doc, state, assets = published(db, settings, monkeypatch)
    revision = db.get(Document, doc).publication_revision
    status = deletion.request_document_deletion(db, doc, settings=settings)
    assert status.status == "deleting"
    item = db.get(Document, doc)
    assert item.current_chunk_set_id is None and item.publication_revision == revision + 1
    job = db.scalar(select(DocumentDeletionJob))
    manifest = DocumentDeletionManifest.from_payload(job.manifest)
    assert manifest.schema_version == 2 and manifest.versioned
    targets = versions.VersionTargets.model_validate(manifest.versioned)
    assert len(targets.graphs) == len(targets.indices) == 1
    assert targets.source_versions == (state["source_version"],)
    # Every stored derived asset, including content-addressed KG/checkpoints,
    # belongs to the persisted deletion prefixes; no global buckets/prefixes.
    assert all(any(key.startswith(prefix) for prefix in manifest.derived_prefixes) for key in assets.data)
    assert deletion.request_document_deletion(db, doc, settings=settings).status == "deleting"
    assert db.scalar(select(func.count()).select_from(DocumentDeletionJob)) == 1
    now = _now(db)
    job.status, job.current_step = "processing", "finalize_postgresql"
    job.locked_by, job.lease_token = "synthetic-delete", uuid4()
    job.locked_at, job.lease_expires_at = now, now + timedelta(seconds=120)
    db.commit()
    claimed = deletion.ClaimedDocumentDeletion.from_job(job)
    deletion.finalize_postgresql_deletion(db, claimed=claimed, manifest=manifest)
    db.commit()
    for model in (Document, DocumentProcessingJob, DocumentChunk, ChunkSet, KGExtractionUnit, GraphBuild, SourceDocumentVersion, DocumentParseRun):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_delete_cancels_queued_work_but_not_running_or_uncertain(db, settings, monkeypatch):
    doc, job_id, assets, _ = parsed(db, settings, monkeypatch)
    settings.document_deletion_executor_enabled = True
    job = db.get(DocumentProcessingJob, job_id)
    job.checkpoint = {**job.checkpoint, "requires_io_reconciliation": True}
    db.commit()
    with pytest.raises(BusinessError) as exc:
        deletion.request_document_deletion(db, doc, settings=settings)
    assert exc.value.code == "DOCUMENT_DELETION_IO_RECONCILIATION_REQUIRED"
    assert db.get(Document, doc).deletion_status == "normal"
    # Synthetic fixture correction only; there is no runtime endpoint that
    # clears an uncertain IO diagnosis without separately authorized review.
    job = db.get(DocumentProcessingJob, job_id)
    job.checkpoint = {"pipeline": job.checkpoint["pipeline"], "parse_run_id": job.checkpoint["parse_run_id"]}
    db.commit()
    deletion.request_document_deletion(db, doc, settings=settings)
    assert db.get(DocumentProcessingJob, job_id).status == "cancelled"
    from app.services.document_processing import advance_processing
    with pytest.raises(BusinessError) as exc:
        advance_processing(db, doc, job_id, settings=settings, worker_id="late", assets=assets)
    assert exc.value.code == "DOCUMENT_DELETION_IN_PROGRESS"


def targets():
    doc, source, graph, chunk = uuid4(), uuid4(), uuid4(), uuid4()
    return versions.VersionTargets(document_id=doc, source_versions=(source,), job_ids=(uuid4(),),
        graphs=(versions.GraphTarget(graph_build_id=graph, source_version=source, graph_id="G",
            source_path=f"documents/{doc}/sources/{source}/graphs/{graph}", payload_sha256="a"*64),),
        indices=(versions.IndexTarget(chunk_set_id=chunk, source_version=source, graph_build_id=graph,
            index_name=versions.index_name(chunk), manifest_sha256="b"*64, embedding_fingerprint="c"*64),))


class Indices:
    def __init__(self, target):
        s = target.indices[0]
        self.name, self.deleted, self.present, self.aliases = s.index_name, [], True, {}
        self.meta = dict(schema_version=2, document_id=str(target.document_id), source_version=str(s.source_version),
            graph_build_id=str(s.graph_build_id), chunk_set_id=str(s.chunk_set_id), manifest_sha256=s.manifest_sha256,
            embedding_fingerprint=s.embedding_fingerprint)
    def exists(self, *, index):
        assert index == self.name
        return self.present
    def get_mapping(self, *, index): return {index: {"mappings": {"_meta": self.meta}}}
    def get_alias(self, *, index): return {index: {"aliases": self.aliases}}
    def delete(self, *, index):
        self.deleted.append(index)
        self.present = False
        return {"acknowledged": True}


def test_exact_index_delete_checks_owner_aliases_and_is_idempotent():
    target = targets()
    client = SimpleNamespace(indices=Indices(target))
    checkpoint = lambda: None
    saved = client.indices.meta["document_id"]
    client.indices.meta["document_id"] = str(uuid4())
    with pytest.raises(BusinessError): versions.delete_versioned_indices(target, client=client, checkpoint=checkpoint)
    assert not client.indices.deleted
    client.indices.meta["document_id"] = saved
    client.indices.aliases = {"protected-current": {}}
    with pytest.raises(BusinessError): versions.delete_versioned_indices(target, client=client, checkpoint=checkpoint)
    assert not client.indices.deleted
    client.indices.aliases = {}
    versions.delete_versioned_indices(target, client=client, checkpoint=checkpoint)
    versions.delete_versioned_indices(target, client=client, checkpoint=checkpoint)
    assert client.indices.deleted == [target.indices[0].index_name]


@pytest.mark.parametrize("mutate", ["graph_path", "index_name", "source", "duplicate"])
def test_manifest_rejects_cross_document_or_wildcard_targets(mutate):
    raw = targets().model_dump(mode="json")
    if mutate == "graph_path": raw["graphs"][0]["source_path"] = "documents/other/"
    elif mutate == "index_name": raw["indices"][0]["index_name"] = "pdf-kg-*"
    elif mutate == "source": raw["indices"][0]["source_version"] = str(uuid4())
    else: raw["graphs"].append(deepcopy(raw["graphs"][0]))
    with pytest.raises(ValueError): versions.VersionTargets.model_validate(raw)


class GraphDriver:
    def __init__(self, target):
        self.graph = {**target.graphs[0].model_dump(mode="json"), "document_id": str(target.document_id), "graph_schema_version": 2}
        self.calls, self.commits, self.conflicts = [], 0, 0
    def session(self, **kwargs): return self
    def begin_transaction(self, **kwargs): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def commit(self): self.commits += 1
    def run(self, query, **params):
        self.calls.append((query, params))
        if query == versions.GRAPH_LOCK: result = {"graph": self.graph} if self.graph else None
        elif query == versions.GRAPH_CHECK: result = {"total": 2, "owned": 2}
        elif query == versions.ENTITY_CHECK: result = {"entities": 2, "conflicts": self.conflicts}
        elif query == versions.GRAPH_DELETE:
            result, self.graph = {}, None
        else: raise AssertionError(query)
        return SimpleNamespace(single=lambda **kwargs: result, consume=lambda: None)


def test_graph_delete_rejects_foreign_owner_or_edges_and_never_scans_all_graphs(settings):
    target, checkpoint = targets(), lambda: None
    driver = GraphDriver(target)
    driver.conflicts = 1
    with pytest.raises(BusinessError): versions.delete_versioned_graphs(target, settings=settings, checkpoint=checkpoint, driver=driver)
    assert not any(q == versions.GRAPH_DELETE for q, _ in driver.calls)
    driver.conflicts = 0
    driver.graph["document_id"] = str(uuid4())
    with pytest.raises(BusinessError): versions.delete_versioned_graphs(target, settings=settings, checkpoint=checkpoint, driver=driver)
    driver.graph["document_id"] = str(target.document_id)
    versions.delete_versioned_graphs(target, settings=settings, checkpoint=checkpoint, driver=driver)
    versions.delete_versioned_graphs(target, settings=settings, checkpoint=checkpoint, driver=driver)
    assert sum(q == versions.GRAPH_DELETE for q, _ in driver.calls) == 1
    assert all(params["graph_id"] == "G" for _, params in driver.calls)


def test_knowledge_update_guards_documents_before_any_mutation(monkeypatch):
    from app.services import knowledge_items
    from app.schemas.knowledge_item import KnowledgeItemUpdate
    item = SimpleNamespace(id=uuid4(), status="draft", title="old", content="body", sources=[],
        source_document_id=uuid4(), source_filename="pdf")
    monkeypatch.setattr(knowledge_items, "get_knowledge_item", lambda *args: item)
    def reject(db, doc_ids):
        assert item.title == "old" and item.source_document_id in doc_ids
        raise BusinessError("DOCUMENT_DELETION_IN_PROGRESS", "deleted", status_code=409)
    monkeypatch.setattr(knowledge_items, "_guard_document_sources", reject)
    with pytest.raises(BusinessError):
        knowledge_items.update_knowledge_item(SimpleNamespace(rollback=lambda: None), item.id, KnowledgeItemUpdate(title="new"))
    assert item.title == "old"
