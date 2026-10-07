"""M3 offline orchestration. SQLite is NOT acceptance of PostgreSQL guards."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.ingestion.sequential_chunker import SegmentationConfig
from app.models import Document, DocumentBlock, DocumentChunk, DocumentParseRun, GraphBuild, KGExtractionUnit, SourceDocumentVersion, DocumentProcessingJob
from app.models.document_chunk_set import ChunkSet
from app.search_engine.versioned_index import VersionedIndex, index_name, payload_digest
from app.services import document_chunk_sets as service
from app.services import document_graph_builds as kg
from app.services.embedding_contract import embedding_fingerprint
from app.services.retrieval_admission import published_targets, filter_published_hits
from app.services.hybrid_search import SearchEngineHit, hybrid_search_chunks
from test_kg_v2_builds import db, settings, prepared, advance, Provider, Writer


class Encoder:
    def __init__(self, db, settings):
        self.db, self.settings, self.calls = db, settings, []
    def encode_documents(self, texts):
        assert not self.db.in_transaction()
        self.calls.append(texts)
        return SimpleNamespace(embeddings=[[float(len(t))] * 1024 for t in texts],
            embedding_model=self.settings.embedding_model, embedding_dim=1024)
    def encode_query(self, query):
        assert not self.db.in_transaction()
        return SimpleNamespace(embeddings=[[0.1] * 1024], embedding_model=self.settings.embedding_model, embedding_dim=1024)


class Index:
    def __init__(self, db):
        self.db, self.calls, self.fail, self.hook = db, [], False, None
    def publish(self, set_id, owner, rows):
        assert not self.db.in_transaction()
        self.calls.append(deepcopy(rows))
        if self.hook:
            self.hook()
        if self.fail:
            raise TimeoutError("sensitive endpoint")
        return {**owner, "schema_version": 2, "chunk_set_id": str(set_id), "chunk_count": len(rows),
            "index_name": index_name(set_id), "payload_sha256": payload_digest(rows), "verified": True, "write_blocked": True}


def ready(db, settings, *, empty=False):
    settings.pdf_kg_chunks_enabled = True
    settings.pdf_kg_embedding_revision = "synthetic-sha256-v1"
    doc, version, build, assets, request = prepared(db, settings,
        "# 6 技术要求\n条款ZL101 ≥350 MPa。\n\n表1\n|牌号|强度 MPa|\n|---|---|\n|ZL101|350|\n|ZL102|400|")
    source = db.get(SourceDocumentVersion, version)
    directory = json.loads(assets.data[source.block_map_object_key])
    for b in directory["blocks"]:
        db.add(DocumentBlock(id=UUID(b["block_id"]), document_id=doc, parse_run_id=source.parse_run_id,
            block_index=b["block_index"], block_type=b["block_type"], text="auxiliary"))
    db.commit()
    provider, writer = Provider(db, [dict(entities=[], relationships=[])] * 2 if empty else None), Writer(db)
    for _ in range(10):
        result = advance(db, settings, doc, build, assets, provider, writer)
        if result["status"] in kg.READY:
            break
    assert result["status"] == ("ready_empty" if empty else "ready")
    return doc, version, build, assets, request, provider, writer


def prepare(db, settings, values, config=None, rechunk=False, request=None):
    doc, source, build, *_ = values
    return service.prepare_chunk_set(db, doc, source, build, request or (uuid4() if rechunk else values[4]),
        config or SegmentationConfig(chunk_size=30, overlap=5), operation="rechunk" if rechunk else "process", settings=settings)


def finish(db, settings, values, result, encoder, index):
    for _ in range(25):
        result = service.advance_chunk_set(db, values[0], result["chunk_set_id"], settings=settings, assets=values[3], provider=encoder, index=index)
        if result["status"] == "indexed":
            return result
    raise AssertionError(result)


def test_three_rechunk_configs_keep_kg_and_history_and_reuse_embeddings(db, settings, monkeypatch):
    values = ready(db, settings)
    doc, version, build, assets, request, model, writer = values
    units_before = [(u.id, u.allocated_anchor_id, u.source_start, u.source_end) for u in db.scalars(select(KGExtractionUnit))]
    graph_before = kg._snapshot(db.get(GraphBuild, build))
    parse_count = db.scalar(select(func.count()).select_from(DocumentParseRun))
    db.commit()
    def forbidden(*args, **kwargs):
        pytest.fail("Rechunk reached KG/parser mutation")
    for module, names in [(kg, ("make_graph_id", "split_units", "extract_piece", "configured_writer", "prepare_graph_build", "advance_graph_build"))]:
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    from app.services import document_parsing
    monkeypatch.setattr(document_parsing, "parse_document", forbidden)
    encoder, index = Encoder(db, settings), Index(db)
    initial = prepare(db, settings, values)
    assert initial["stage"] == "kg_ready" and initial["chunk_count"] is None
    assert prepare(db, settings, values)["chunk_set_id"] == initial["chunk_set_id"]
    result = finish(db, settings, values, initial, encoder, index)
    old_chunks = [(c.id, c.content, c.source_metadata, c.embedding_status) for c in service._chunks(db, result["chunk_set_id"])]
    db.commit()
    count = len(encoder.calls)
    # Identical source/config reuses every vector; creates independent chunk identities.
    repeated = finish(db, settings, values, prepare(db, settings, values, rechunk=True), encoder, index)
    assert len(encoder.calls) == count
    for config in [SegmentationConfig(chunk_size=17, overlap=0, boundary="characters"),
                   SegmentationConfig(chunk_size=42, overlap=9, boundary="line")]:
        result = finish(db, settings, values, prepare(db, settings, values, config, True), encoder, index)
    assert result["publication_revision"] == 4
    assert kg._snapshot(db.get(GraphBuild, build)) == graph_before
    assert [(u.id, u.allocated_anchor_id, u.source_start, u.source_end) for u in db.scalars(select(KGExtractionUnit))] == units_before
    assert db.scalar(select(func.count()).select_from(DocumentParseRun)) == parse_count
    assert [(c.id, c.content, c.source_metadata, c.embedding_status) for c in service._chunks(db, initial["chunk_set_id"])] == old_chunks
    assert len(model.calls) == 2 and len(writer.calls) == 1
    assert repeated["chunk_set_id"] != initial["chunk_set_id"]
    for rows in index.calls:
        assert any(r["source_metadata"]["kg_refs"] for r in rows)
        assert all("kg-anchor" not in r["content"] and r["content"] == r["content_max"] == r["content_smart"] for r in rows)


def test_index_failure_keeps_published_pointer_and_explicit_retry(db, settings):
    values = ready(db, settings)
    encoder, index = Encoder(db, settings), Index(db)
    first = finish(db, settings, values, prepare(db, settings, values), encoder, index)
    new = prepare(db, settings, values, SegmentationConfig(chunk_size=18, overlap=4), True)
    index.fail = True
    with pytest.raises(BusinessError) as exc:
        finish(db, settings, values, new, encoder, index)
    assert exc.value.code == "CHUNK_INDEXING_FAILED" and "sensitive" not in str(exc.value)
    assert db.get(Document, values[0]).current_chunk_set_id == first["chunk_set_id"]
    db.commit()
    with pytest.raises(BusinessError) as exc:
        service.advance_chunk_set(db, values[0], new["chunk_set_id"], settings=settings, assets=values[3])
    assert exc.value.code == "CHUNK_EXPLICIT_RETRY_REQUIRED"
    db.rollback()
    index.fail = False
    before = len(encoder.calls)
    result = service.advance_chunk_set(db, values[0], new["chunk_set_id"], retry=True, settings=settings, assets=values[3], provider=encoder, index=index)
    assert result["is_current"] and len(encoder.calls) == before


def test_mapping_conflict_keeps_stage_error_and_logs_cause_without_publishing(db, settings, monkeypatch, caplog):
    from test_versioned_index import Client
    values = ready(db, settings)
    result = prepare(db, settings, values)
    client = Client(normalize=True)
    original = client.get_mapping
    def bad_dimension(*, index):
        response = original(index=index)
        response[index]["mappings"]["properties"]["embedding"]["dimension"] = 768
        return response
    monkeypatch.setattr(client, "get_mapping", bad_dimension)
    with pytest.raises(BusinessError) as caught:
        finish(db, settings, values, result, Encoder(db, settings), VersionedIndex(settings, client))
    assert caught.value.code == "CHUNK_INDEXING_FAILED"
    assert str(caught.value.__cause__) == "Index identity/mapping conflict"
    assert client.calls == ["create"] and not client.rows
    assert db.get(Document, values[0]).current_chunk_set_id is None
    assert db.get(ChunkSet, result["chunk_set_id"]).last_error_code == "CHUNK_INDEXING_FAILED"
    record, = [r for r in caplog.records if getattr(r, "event", None) == "chunk_set_indexing_failed"]
    assert record.operation == "index_publish" and record.cause_type == "ValueError"
    assert record.index_name == index_name(result["chunk_set_id"]) and record.http_status is None
    assert record.document_id == str(values[0]) and record.chunk_set_id == str(result["chunk_set_id"])
    mapping, = [r for r in caplog.records if getattr(r, "event", None) == "versioned_index_failed"]
    assert mapping.mismatch_path == "properties.embedding.dimension"
    assert mapping.expected_type == mapping.actual_type == "int" and record.exc_info is None
    assert "indexing_diagnostics=" in record.getMessage()


def test_deleted_or_expired_worker_cannot_publish(db, settings):
    values = ready(db, settings)
    encoder, index = Encoder(db, settings), Index(db)
    new = prepare(db, settings, values)
    def expire():
        job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.chunk_set_id == new["chunk_set_id"]))
        job.lease_expires_at = kg._now(db) - timedelta(seconds=1)
        db.commit()
    index.hook = expire
    with pytest.raises(BusinessError, match="切片"):
        finish(db, settings, values, new, encoder, index)
    assert db.get(Document, values[0]).current_chunk_set_id is None


def test_admission_excludes_oldsets_corruption_and_missing_no_fallback(db, settings):
    values = ready(db, settings)
    encoder, index = Encoder(db, settings), Index(db)
    first = finish(db, settings, values, prepare(db, settings, values), encoder, index)
    old = index.calls[-1]
    final = finish(db, settings, values, prepare(db, settings, values, rechunk=True), encoder, index)
    factory = lambda: Session(db.get_bind())
    targets = published_targets(factory, settings)
    assert targets == {str(final["chunk_set_id"]): index_name(final["chunk_set_id"])}
    hits = [SearchEngineHit(r["chunk_id"], 1, r, index_name(first["chunk_set_id"])) for r in old]
    hits += [SearchEngineHit(r["chunk_id"], 1, r, index_name(final["chunk_set_id"])) for r in index.calls[-1]]
    corrupt = deepcopy(hits[-1].source); corrupt["source_metadata"] = {"kg_refs": []}
    hits.append(SearchEngineHit(corrupt["chunk_id"], 1, corrupt, index_name(final["chunk_set_id"])))
    kept, _ = filter_published_hits(hits, [], factory, settings, targets)
    assert len(kept) == len(index.calls[-1])
    settings.pdf_kg_search_enabled = True
    result = hybrid_search_chunks(db, query="test", document_id=uuid4(), settings=settings,
        deletion_filter_session_factory=factory, client=object(), embedding_provider=object())
    assert result.items == []


def test_identity_changes_fail_without_new_calls(db, settings):
    values = ready(db, settings)
    result = prepare(db, settings, values)
    with pytest.raises(BusinessError) as exc:
        prepare(db, settings, values, SegmentationConfig(chunk_size=50, overlap=0))
    assert exc.value.code == "CHUNK_REQUEST_CONFLICT"
    db.rollback()
    settings.pdf_kg_embedding_revision = "changed"
    with pytest.raises(BusinessError) as exc:
        service.advance_chunk_set(db, values[0], result["chunk_set_id"], settings=settings)
    assert exc.value.code == "CHUNK_EMBEDDING_IDENTITY_CHANGED"


def test_history_reference_preserved_current_listing_and_legacy_write_guards(db, settings):
    from app.models import KnowledgeItem, KnowledgeItemSource, KnowledgeItemChunk
    from app.services.document_parsing import list_document_chunks
    from app.services.embeddings import generate_document_embeddings
    from app.services.search_index import _rebuild_one_document
    values = ready(db, settings)
    encoder, index = Encoder(db, settings), Index(db)
    initial = finish(db, settings, values, prepare(db, settings, values), encoder, index)
    chunk = service._chunks(db, initial["chunk_set_id"])[0]
    old_id, old_content = chunk.id, chunk.content
    item = KnowledgeItem(item_type="text", title="historical", content=old_content, content_hash="a" * 64, source_document_id=values[0])
    db.add(item); db.flush()
    db.add(KnowledgeItemSource(knowledge_item_id=item.id, document_id=values[0])); db.flush()
    link = KnowledgeItemChunk(knowledge_item_id=item.id, document_id=values[0], chunk_id=old_id, chunk_index=chunk.chunk_index, source_text=old_content)
    db.add(link); db.commit()
    final = finish(db, settings, values, prepare(db, settings, values, SegmentationConfig(chunk_size=20, overlap=0), True), encoder, index)
    assert db.get(DocumentChunk, old_id).content == old_content and link.chunk_id == old_id
    assert all(c.chunk_set_id == final["chunk_set_id"] for c in list_document_chunks(db, values[0]).items)
    assert list_document_chunks(db, values[0], chunk_set_id=initial["chunk_set_id"]).items[0].id == old_id
    with pytest.raises(BusinessError) as exc:
        generate_document_embeddings(db, values[0])
    assert exc.value.code == "DOCUMENT_VERSIONED_PIPELINE_REQUIRED"
    db.rollback()
    with pytest.raises(BusinessError) as exc:
        _rebuild_one_document(db, document_id=values[0], client=object(), index_name="legacy", settings=settings, batch_size=8, delete_existing=True)
    assert exc.value.code == "DOCUMENT_VERSIONED_PIPELINE_REQUIRED"


def test_hybrid_queries_only_published_indices_before_topk_and_rechecks_switch(db, settings):
    values = ready(db, settings)
    encoder, index = Encoder(db, settings), Index(db)
    result = finish(db, settings, values, prepare(db, settings, values), encoder, index)
    factory = lambda: Session(db.get_bind())
    settings.pdf_kg_search_enabled = True
    class Search:
        calls = []
        def search(self, **kwargs):
            self.calls.append(kwargs)
            assert kwargs["index"] == [index_name(result["chunk_set_id"])]
            body = kwargs["body"]
            filters = body["query"]["bool"]["filter"] if "bool" in body["query"] else body["query"]["knn"]["embedding"]["filter"]["bool"]["filter"]
            assert {"terms": {"chunk_set_id": [str(result["chunk_set_id"])]}} in filters
            return {"hits": {"hits": [dict(_id=r["chunk_id"], _index=index_name(result["chunk_set_id"]), _source=r, _score=1) for r in index.calls[-1]]}}
    client = Search()
    output = hybrid_search_chunks(db, query="ZL101", settings=settings, embedding_provider=encoder, client=client, deletion_filter_session_factory=factory)
    assert output.items and len(client.calls) == 2
    assert all(c.chunk_set_id == str(result["chunk_set_id"]) and c.source_version == str(values[1]) for c in output.items)
    assert output.items[0].retrieval_source == "both"


def test_source_corruption_cannot_publish(db, settings):
    values = ready(db, settings)
    result = prepare(db, settings, values)
    row = db.get(SourceDocumentVersion, values[1]); key = row.canonical_object_key
    db.commit()
    values[3].data[key] += b"corrupt"
    with pytest.raises(BusinessError) as exc:
        service.advance_chunk_set(db, values[0], result["chunk_set_id"], settings=settings, assets=values[3])
    assert exc.value.code == "CHUNK_CHUNKING_FAILED"
    assert not service._chunks(db, result["chunk_set_id"])
    assert db.get(Document, values[0]).current_chunk_set_id is None


def test_empty_graph_permits_chunks_but_failed_graph_does_not(db, settings):
    values = ready(db, settings, empty=True)
    failed_doc, failed_source, failed_build, *_ = prepared(db, settings, filename="failed-other.pdf")
    with pytest.raises(BusinessError) as exc:
        service.prepare_chunk_set(db, failed_doc, failed_source, failed_build, uuid4(), SegmentationConfig(), settings=settings)
    assert exc.value.code == "CHUNK_GRAPH_NOT_READY"
    db.rollback()
    encoder, index = Encoder(db, settings), Index(db)
    finish(db, settings, values, prepare(db, settings, values), encoder, index)
    assert all(r["source_metadata"] == {"kg_refs": []} for r in index.calls[-1])


def test_embedding_partial_retry_retains_completed_batch(db, settings):
    values = ready(db, settings)
    settings.embedding_batch_size = 1
    result = prepare(db, settings, values)
    encoder, index = Encoder(db, settings), Index(db)
    for _ in range(2):
        result = service.advance_chunk_set(db, values[0], result["chunk_set_id"], settings=settings, assets=values[3], provider=encoder, index=index)
    assert result["embedding_counts"]["embedded"] == 1
    class Broken:
        def encode_documents(self, texts):
            return SimpleNamespace(embedding_model=settings.embedding_model, embedding_dim=1024, embeddings=[[float("nan")] * 1024])
    with pytest.raises(BusinessError) as exc:
        service.advance_chunk_set(db, values[0], result["chunk_set_id"], settings=settings, assets=values[3], provider=Broken(), index=index)
    assert exc.value.code == "CHUNK_EMBEDDING_FAILED" and not index.calls
    result = service.advance_chunk_set(db, values[0], result["chunk_set_id"], retry=True, settings=settings, assets=values[3], provider=encoder, index=index)
    finish(db, settings, values, result, encoder, index)
    assert encoder.calls.count(encoder.calls[0]) == 1


def test_deletion_admission_and_late_worker_guard(db, settings):
    from app.services.document_deletion import request_document_deletion
    values = ready(db, settings)
    result = prepare(db, settings, values)
    with pytest.raises(BusinessError):
        request_document_deletion(db, values[0], settings=settings)
    db.rollback()
    encoder, index = Encoder(db, settings), Index(db)
    def deleting():
        row = db.get(Document, values[0]); row.deletion_status = "deleting"; db.commit()
    index.hook = deleting
    with pytest.raises(BusinessError) as exc:
        finish(db, settings, values, result, encoder, index)
    assert exc.value.code == "DOCUMENT_DELETION_IN_PROGRESS"
    assert db.get(Document, values[0]).current_chunk_set_id is None


def test_failed_old_request_cannot_replace_a_newer_published_version(db, settings):
    values = ready(db, settings)
    encoder, index = Encoder(db, settings), Index(db)
    finish(db, settings, values, prepare(db, settings, values), encoder, index)
    stale = prepare(db, settings, values, rechunk=True)
    index.fail = True
    with pytest.raises(BusinessError):
        finish(db, settings, values, stale, encoder, index)
    index.fail = False
    latest = finish(db, settings, values, prepare(db, settings, values, rechunk=True), encoder, index)
    with pytest.raises(BusinessError) as exc:
        service.advance_chunk_set(db, values[0], stale["chunk_set_id"], retry=True, settings=settings, assets=values[3], provider=encoder, index=index)
    assert exc.value.code == "CHUNK_PUBLICATION_CONFLICT"
    assert db.get(Document, values[0]).current_chunk_set_id == latest["chunk_set_id"]


def test_failed_first_segmentation_can_start_explicit_new_set_without_rebinding(db, settings):
    values = ready(db, settings)
    settings.pdf_kg_max_chunks = 2
    bad = prepare(db, settings, values, SegmentationConfig(chunk_size=5, overlap=0))
    with pytest.raises(BusinessError):
        service.advance_chunk_set(db, values[0], bad["chunk_set_id"], settings=settings, assets=values[3])
    original_job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.chunk_set_id == bad["chunk_set_id"]))
    original_id, original_request = original_job.id, original_job.request_id
    db.commit()
    replacement = prepare(db, settings, values, SegmentationConfig(chunk_size=500, overlap=0), True)
    result = finish(db, settings, values, replacement, Encoder(db, settings), Index(db))
    assert result["is_current"] and result["chunk_set_id"] != bad["chunk_set_id"]
    original = db.get(DocumentProcessingJob, original_id)
    assert original.request_id == original_request and original.status == "failed" and original.chunk_set_id == bad["chunk_set_id"]


def test_service_closes_owned_index_transport_outside_sql(db, settings, monkeypatch):
    values = ready(db, settings)
    class OwnedIndex(Index):
        closed = False
        def close(self):
            assert not self.db.in_transaction()
            self.closed = True
    index = OwnedIndex(db)
    monkeypatch.setattr(service, "VersionedIndex", lambda _: index)
    finish(db, settings, values, prepare(db, settings, values), Encoder(db, settings), None)
    assert index.closed and len(index.calls) == 1
