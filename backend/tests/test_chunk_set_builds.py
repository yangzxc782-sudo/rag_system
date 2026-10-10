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
from app.ingestion.block_chunker import BlockChunkerConfig
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
        config or BlockChunkerConfig(min_chunk_chars=0, max_chunk_chars=30, overlap_chars=5), operation="rechunk" if rechunk else "process", settings=settings)


def finish(db, settings, values, result, encoder, index):
    for _ in range(25):
        result = service.advance_chunk_set(db, values[0], result["chunk_set_id"], settings=settings, assets=values[3], provider=encoder, index=index)
        if result["status"] == "indexed":
            return result
    raise AssertionError(result)


def chunk_snapshot(chunk):
    result = kg._snapshot(chunk)
    if result["embedding"] is not None:
        result["embedding"] = list(result["embedding"])
    return result


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
    for config in [BlockChunkerConfig(min_chunk_chars=0, max_chunk_chars=17, overlap_chars=0),
                   BlockChunkerConfig(min_chunk_chars=0, max_chunk_chars=42, overlap_chars=9)]:
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
    new = prepare(db, settings, values, BlockChunkerConfig(min_chunk_chars=0, max_chunk_chars=18, overlap_chars=4), True)
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
        prepare(db, settings, values, BlockChunkerConfig(min_chunk_chars=0, max_chunk_chars=50, overlap_chars=0))
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
    final = finish(db, settings, values, prepare(db, settings, values, BlockChunkerConfig(min_chunk_chars=0, max_chunk_chars=20, overlap_chars=0), True), encoder, index)
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
        service.prepare_chunk_set(db, failed_doc, failed_source, failed_build, uuid4(), BlockChunkerConfig(), settings=settings)
    assert exc.value.code == "CHUNK_GRAPH_NOT_READY"
    db.rollback()
    encoder, index = Encoder(db, settings), Index(db)
    finish(db, settings, values, prepare(db, settings, values), encoder, index)
    assert all(r["source_metadata"]["kg_refs"] == [] for r in index.calls[-1])


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
    bad = prepare(db, settings, values, BlockChunkerConfig(min_chunk_chars=0, max_chunk_chars=5, overlap_chars=0))
    with pytest.raises(BusinessError):
        service.advance_chunk_set(db, values[0], bad["chunk_set_id"], settings=settings, assets=values[3])
    original_job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.chunk_set_id == bad["chunk_set_id"]))
    original_id, original_request = original_job.id, original_job.request_id
    db.commit()
    replacement = prepare(db, settings, values, BlockChunkerConfig(min_chunk_chars=0, max_chunk_chars=500, overlap_chars=0), True)
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


def structured_ready(db, settings, *, section_path=None):
    """Synthetic S1 source with real structural blocks; only SQLite/fake gateways."""
    from app.ingestion.frozen_source import render_frozen_source
    from test_kg_v2_builds import source, MemoryAssets
    from test_frozen_source import block
    settings.pdf_kg_chunks_enabled = True
    settings.pdf_kg_embedding_revision = "synthetic-structure-v3"
    assets = MemoryAssets(db)
    doc, version = source(db, assets)
    row = db.get(SourceDocumentVersion, version)
    prefix = f"source/{version}"
    blocks = [
        block(0, "# 6 材料", kind="title"),
        block(1, "普通正文参数与条件。 " * 620),
        block(2, "材料性能", kind="title", section_path=["6 材料"]),
        block(3, "|材料|条件|\n|---|---|\n" + "|合成材料|合成条件|\n" * 550, kind="table", page=1,
              asset_keys=[f"{prefix}/tables/table.json"]),
        block(4, "x+y=" + "f" * 2000, kind="formula", page=2),
        block(5, "# 7 其他", kind="title", page=3),
        block(6, "另一个章节的普通正文。 " * 450, page=3),
    ]
    if section_path is not None:
        blocks[0].section_path = list(section_path)
        blocks[0].text = section_path[-1]
    for b in blocks:
        b.document_id, b.parse_run_id = doc, row.parse_run_id
    frozen = render_frozen_source(blocks, document_id=doc, parse_run_id=row.parse_run_id, source_version=version,
        output_prefix=prefix, registered_asset_keys=[f"{prefix}/tables/table.json"])
    assets.data[row.canonical_object_key], assets.data[row.block_map_object_key] = frozen.canonical, frozen.block_map
    row.canonical_sha256, row.block_map_sha256, row.character_count = frozen.canonical_sha256, frozen.block_map_sha256, frozen.character_count
    for b in blocks:
        db.add(DocumentBlock(id=b.id, document_id=doc, parse_run_id=row.parse_run_id, block_index=b.block_index,
            block_key=b.block_key, block_type=b.block_type, text=b.text))
    db.commit()
    request = uuid4()
    build = kg.prepare_graph_build(db, doc, version, request, settings=settings, assets=assets)["graph_build_id"]
    model, writer = Provider(db), Writer(db)
    for _ in range(40):
        result = advance(db, settings, doc, build, assets, model, writer)
        if result["status"] in kg.READY:
            break
    assert result["status"] == "ready"
    return (doc, version, build, assets, request, model, writer), frozen


def test_s3_abc_structural_rechunk_manifest_sql_index_and_history(db, settings, monkeypatch):
    from app.ingestion.block_chunker import SEGMENTATION_VERSION
    from app.ingestion.frozen_source import json_bytes, sha256_bytes
    from app.models import DocumentChunkBlock
    from app.schemas.conversation_rag import CitationPayload
    from test_graph_sources_v2 import context_for
    from app.services.graph_sources import GraphSourceAuthority
    from app.services import document_parsing
    from unittest.mock import Mock

    values, frozen = structured_ready(db, settings)
    doc, version, build, assets, _, model, writer = values
    before_source = kg._snapshot(db.get(SourceDocumentVersion, version))
    before_graph = kg._snapshot(db.get(GraphBuild, build))
    before_units = [kg._snapshot(u) for u in db.scalars(select(KGExtractionUnit).order_by(KGExtractionUnit.unit_index))]
    initial_calls = len(model.calls), len(writer.calls)
    # Any subsequent mutation-path call is forbidden, including accidental fallback.
    guard = Mock(side_effect=AssertionError("Rechunk reached parsing/KG/writer"))
    monkeypatch.setattr(document_parsing, "parse_document", guard)
    for name in ("prepare_graph_build", "advance_graph_build", "extract_piece", "configured_writer"):
        monkeypatch.setattr(kg, name, guard)
    monkeypatch.setattr(model, "generate", guard)
    monkeypatch.setattr(writer, "write", guard)
    # These mutable auxiliary values must never replace frozen structural metadata.
    for block in db.scalars(select(DocumentBlock)):
        block.text, block.block_type, block.page_start, block.section_path = "changed auxiliary", "unknown", 99, ["changed"]
    db.commit()
    configs = [
        BlockChunkerConfig(),
        BlockChunkerConfig(max_chunk_chars=1200, min_chunk_chars=200, overlap_chars=120),
        BlockChunkerConfig(max_chunk_chars=3000, min_chunk_chars=200, overlap_chars=100),
    ]
    encoder, index = Encoder(db, settings), Index(db)
    sets, intervals = [], []
    for i, config in enumerate(configs):
        state = prepare(db, settings, values, config, rechunk=i > 0)
        parent = db.get(ChunkSet, state["chunk_set_id"])
        source_data, row_data = deepcopy(before_source), kg._snapshot(parent)
        assert parent.segmentation_config == config.model_dump() and parent.segmentation_config_sha256 == config.fingerprint
        assert parent.segmentation_version == state["segmentation_version"] == SEGMENTATION_VERSION
        assert len(parent.segmentation_config) == 6
        db.commit()
        drafts, manifest = service._drafts(source_data, row_data, kg.load_anchor_index(db, doc, version, build), assets, settings)
        # Independent recomputation (including JSONB-style key reordering) is identical.
        row_data["segmentation_config"] = dict(reversed(list(row_data["segmentation_config"].items())))
        again, same = service._drafts(source_data, row_data, kg.load_anchor_index(db, doc, version, build), assets, settings)
        assert drafts == again and json_bytes(manifest) == json_bytes(same)
        assert manifest["schema_version"] == 3 and manifest["source_map_sha256"] == frozen.block_map_sha256
        assert manifest["canonical_sha256"] == frozen.canonical_sha256 and manifest["segmentation_config"] == config.model_dump()
        assert manifest["segmentation_version"] == SEGMENTATION_VERSION
        state = finish(db, settings, values, state, encoder, index)
        parent = db.get(ChunkSet, state["chunk_set_id"])
        assert parent.manifest_sha256 == sha256_bytes(json_bytes(manifest))
        assert assets.data[parent.manifest_object_key] == json_bytes(manifest)
        rows = [chunk_snapshot(c) for c in service._chunks(db, parent.id)]
        service._verify_chunks(rows, drafts, manifest)
        service._verify_links(db, parent.id, manifest)
        links = list(db.scalars(select(DocumentChunkBlock).join(DocumentChunk).where(DocumentChunk.chunk_set_id == parent.id)))
        assert len(links) == sum(len(d.source_metadata["block_ids"]) for d in drafts)
        by_id = {p["chunk_id"]: p for p in index.calls[-1]}
        for row, entry in zip(rows, manifest["chunks"], strict=True):
            assert row["content"] == frozen.canonical.decode()[row["source_start"]:row["source_end"]]
            assert by_id[str(row["id"])]["source_metadata"] == entry["source_metadata"] == row["source_metadata"]
            assert all(by_id[str(row["id"])][k] == entry[k] for k in
                ("chunk_type", "section_title", "content_format", "chunk_method", "token_count", "page_start", "page_end", "parse_run_id"))
            assert isinstance(row["source_metadata"]["kg_refs"], list)
        assert any(row["source_metadata"]["kg_refs"] for row in rows)
        assert any(r["chunk_type"] == "table" and len(r["content"]) > 4000 for r in rows)
        assert any("formula" in r["source_metadata"]["block_types"] and len(r["content"]) > 1800 for r in rows)
        assert all(r["page_start"] != 99 and r["section_title"] != "changed" for r in rows)
        context = context_for(db, settings, parent.id, budget=100000)
        bindings = GraphSourceAuthority(settings).resolve_in_session(db, context, current=True)
        assert bindings and all(a.fully_covered for a in bindings)
        for citation in context.chunks:
            assert CitationPayload.model_validate_json(CitationPayload(chunk=citation).model_dump_json()).chunk == citation
        sets.append((parent.id, context, rows))
        intervals.append([(r["source_start"], r["source_end"]) for r in rows])
        db.commit()
    assert intervals[0] != intervals[1] != intervals[2]
    assert len(model.calls) == initial_calls[0] and len(writer.calls) == initial_calls[1]
    guard.assert_not_called()
    assert kg._snapshot(db.get(SourceDocumentVersion, version)) == before_source
    assert kg._snapshot(db.get(GraphBuild, build)) == before_graph
    assert [kg._snapshot(u) for u in db.scalars(select(KGExtractionUnit).order_by(KGExtractionUnit.unit_index))] == before_units
    assert db.scalar(select(func.count()).select_from(DocumentParseRun)) == 1
    assert db.get(Document, doc).publication_revision == 3
    for set_id, context, stored in sets:
        assert [chunk_snapshot(c) for c in service._chunks(db, set_id)] == stored
        assert GraphSourceAuthority(settings).resolve_in_session(db, context, current=False)


@pytest.mark.parametrize("field,value", [
    ("chunk_type", "table"), ("section_title", "tampered"), ("content_format", "mixed"),
    ("chunk_method", "sequential"), ("token_count", -1), ("page_start", 99), ("page_end", 99),
    ("source_metadata", {"kg_refs": []}),
])
def test_s3_sealed_sql_structure_corruption_stops_before_embedding(db, settings, field, value):
    values = ready(db, settings)
    state = service.advance_chunk_set(db, values[0], prepare(db, settings, values)["chunk_set_id"],
        settings=settings, assets=values[3])
    chunk = service._chunks(db, state["chunk_set_id"])[0]
    setattr(chunk, field, value)
    db.commit()  # SQLite deliberately permits corruption that PG guards would reject.
    encoder, index = Encoder(db, settings), Index(db)
    with pytest.raises(BusinessError) as caught:
        service.advance_chunk_set(db, values[0], state["chunk_set_id"], settings=settings, assets=values[3], provider=encoder, index=index)
    assert caught.value.code == "CHUNK_EMBEDDING_FAILED"
    assert not encoder.calls and not index.calls


def test_s3_sealed_block_link_corruption_rejected(db, settings):
    from app.models import DocumentChunkBlock
    values = ready(db, settings)
    state = service.advance_chunk_set(db, values[0], prepare(db, settings, values)["chunk_set_id"],
        settings=settings, assets=values[3])
    link = db.scalar(select(DocumentChunkBlock)); link.block_order += 1
    db.commit()
    with pytest.raises(BusinessError) as caught:
        service.advance_chunk_set(db, values[0], state["chunk_set_id"], settings=settings, assets=values[3], provider=Encoder(db, settings))
    assert caught.value.code == "CHUNK_EMBEDDING_FAILED"


@pytest.mark.parametrize("field,value", [
    ("chunk_type", "wrong"), ("section_title", "wrong"), ("content_format", "wrong"), ("chunk_method", "wrong"),
    ("token_count", 999), ("page_start", 99), ("page_end", 99), ("parse_run_id", str(UUID(int=0))),
])
def test_s3_retrieval_admission_rejects_structural_index_mismatch(db, settings, field, value):
    values = ready(db, settings)
    encoder, index = Encoder(db, settings), Index(db)
    state = finish(db, settings, values, prepare(db, settings, values), encoder, index)
    rows = deepcopy(index.calls[-1])
    bad = deepcopy(rows[0]); bad[field] = value
    name = index_name(state["chunk_set_id"])
    factory = lambda: Session(db.get_bind())
    hits = [SearchEngineHit(r["chunk_id"], 1, r, name) for r in rows + [bad]]
    kept, _ = filter_published_hits(hits, [], factory, settings, published_targets(factory, settings))
    assert len(kept) == len(rows)


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("source_map_sha256", "0" * 64),
    ("segmentation_version", "sequential-codepoints-v1"),
])
def test_s3_manifest_contract_corruption_rejected_even_with_matching_asset_hash(db, settings, field, value):
    from app.ingestion.frozen_source import json_bytes, sha256_bytes
    values = ready(db, settings)
    state = service.advance_chunk_set(db, values[0], prepare(db, settings, values)["chunk_set_id"],
        settings=settings, assets=values[3])
    parent = db.get(ChunkSet, state["chunk_set_id"])
    manifest = json.loads(values[3].data[parent.manifest_object_key])
    manifest[field] = value
    raw = json_bytes(manifest)
    values[3].data[parent.manifest_object_key] = raw
    parent.manifest_sha256 = sha256_bytes(raw)
    db.commit()
    with pytest.raises(BusinessError) as caught:
        service.advance_chunk_set(db, values[0], state["chunk_set_id"], settings=settings, assets=values[3])
    assert caught.value.code == "CHUNK_EMBEDDING_FAILED"


def test_s3_interval_mapping_is_strict_many_to_many_and_rejects_conflicting_identity():
    from app.ingestion.chunk_anchors import anchor_intervals, refs_for_interval
    from test_kg_v2_protocol_units import anchor
    owner = dict(document_id=uuid4(), source_version=uuid4(), graph_build_id=uuid4())
    refs = [{**owner, "source_start": 15, "source_end": 45, "anchor_metadata": anchor("clause")},
            {**owner, "source_start": 30, "source_end": 60, "anchor_metadata": anchor("table")}]
    intervals = anchor_intervals(refs + refs, **owner, character_count=90)
    assert refs_for_interval(0, 15, intervals) == []  # Touch only.
    assert refs_for_interval(60, 90, intervals) == []
    assert refs_for_interval(0, 30, intervals) == [refs[0]["anchor_metadata"]]
    assert refs_for_interval(30, 45, intervals) == [r["anchor_metadata"] for r in refs]
    assert refs_for_interval(45, 60, intervals) == [refs[1]["anchor_metadata"]]
    # Mapping requires intersection, not whole-unit coverage.
    assert refs_for_interval(16, 17, intervals) == [refs[0]["anchor_metadata"]]
    assert refs_for_interval(0, 90, ()) == []
    conflict = {**refs[0], "source_end": 46}
    with pytest.raises(ValueError, match="Conflicting"):
        anchor_intervals([*refs, conflict], **owner, character_count=90)
    for field in owner:
        with pytest.raises(ValueError, match="another"):
            anchor_intervals([{**refs[0], field: uuid4()}], **owner, character_count=90)
    for start, end in [(True, 30), (0, 91), (30, 30)]:
        with pytest.raises(ValueError, match="interval"):
            anchor_intervals([{**refs[0], "source_start": start, "source_end": end}], **owner, character_count=90)


@pytest.mark.parametrize("corruption", ["missing", "unknown_version", "changed_budget", "changed_checkpoint"])
def test_s3_frozen_chunk_contract_never_silently_redefaults(db, settings, corruption):
    values = ready(db, settings)
    state = prepare(db, settings, values)
    parent = db.get(ChunkSet, state["chunk_set_id"])
    job = db.get(DocumentProcessingJob, state["job_id"])
    if corruption == "missing":
        parent.segmentation_config = {k: v for k, v in parent.segmentation_config.items() if k != "min_chunk_chars"}
    elif corruption == "unknown_version":
        parent.segmentation_version = "sequential-codepoints-v1"
    elif corruption == "changed_budget":
        parent.segmentation_config = BlockChunkerConfig().model_dump()
        parent.segmentation_config_sha256 = BlockChunkerConfig().fingerprint
    else:
        job.checkpoint = {**job.checkpoint, "chunk_input_sha256": "0" * 64}
    db.commit()
    before_events = list(values[3].events)
    with pytest.raises(BusinessError) as caught:
        service.advance_chunk_set(db, values[0], state["chunk_set_id"], settings=settings, assets=values[3])
    assert caught.value.code == "CHUNK_REQUEST_CONFLICT"
    assert values[3].events == before_events
    assert not service._chunks(db, state["chunk_set_id"])


def test_s3_versioned_index_readback_verifies_full_structural_payload(db, settings):
    from test_versioned_index import Client
    values = ready(db, settings)
    client = Client(normalize=True)
    gateway = VersionedIndex(settings, client)
    state = finish(db, settings, values, prepare(db, settings, values), Encoder(db, settings), gateway)
    parent = db.get(ChunkSet, state["chunk_set_id"])
    assert parent.status == "indexed" and client.blocked
    assert client.calls == ["create", "bulk", "seal"]
    stored = client.rows
    chunk = service._chunks(db, state["chunk_set_id"])[0]
    payload = stored[str(chunk.id)]
    assert payload["chunk_method"] == chunk.chunk_method
    assert payload["content_format"] == chunk.content_format
    assert payload["source_metadata"] == chunk.source_metadata
    owner = {k: parent.index_receipt[k] for k in
        ("document_id", "source_version", "graph_build_id", "manifest_sha256", "embedding_fingerprint")}
    intended = deepcopy(list(stored.values()))
    payload["source_metadata"]["section_path"] = ["corrupt"]
    with pytest.raises(ValueError, match="payload"):
        gateway.publish(parent.id, owner, intended)  # Sealed readback cannot hide changed structure.
    assert client.calls == ["create", "bulk", "seal"]


def test_s3_json_object_key_reordering_keeps_manifest_and_publication(db, settings):
    from app.ingestion.frozen_source import json_bytes
    from graph_v2_support import reorder_objects
    values = ready(db, settings)
    state = service.advance_chunk_set(db, values[0], prepare(db, settings, values)["chunk_set_id"],
        settings=settings, assets=values[3])
    parent = db.get(ChunkSet, state["chunk_set_id"])
    saved = values[3].data[parent.manifest_object_key]
    digest = parent.manifest_sha256
    # Explicit offline JSONB-shaped simulation, not real PostgreSQL verification.
    parent.segmentation_config = reorder_objects(parent.segmentation_config)
    for chunk in service._chunks(db, parent.id):
        before = deepcopy(chunk.source_metadata)
        chunk.source_metadata = reorder_objects(json.loads(json_bytes(before)))
        assert chunk.source_metadata == before and json_bytes(chunk.source_metadata) == json_bytes(before)
    job = db.get(DocumentProcessingJob, state["job_id"])
    job.checkpoint = reorder_objects(job.checkpoint)
    db.commit()
    final = finish(db, settings, values, state, Encoder(db, settings), Index(db))
    parent = db.get(ChunkSet, final["chunk_set_id"])
    assert parent.status == "indexed" and parent.manifest_sha256 == digest
    assert values[3].data[parent.manifest_object_key] == saved


def test_s3_search_selects_all_strict_structural_source_fields():
    from app.services.hybrid_search import _source_fields
    from app.services.retrieval_admission import STRUCTURE_FIELDS
    assert set(STRUCTURE_FIELDS) <= set(_source_fields())


@pytest.mark.parametrize("intact", [True, False])
def test_s4_table_fragment_flag_defaults_and_chunk_list_projection(db, settings, intact):
    from app.schemas.document_chunk import DocumentChunkRead
    from app.ingestion.block_chunker import SEGMENTATION_VERSION
    from app.services.document_processing import list_processing_jobs
    values, frozen = structured_ready(db, settings)
    state = prepare(db, settings, values, BlockChunkerConfig(keep_table_intact=intact))
    for listing in (service.list_chunk_sets(db, values[0]), list_processing_jobs(db, values[0], settings=settings)):
        assert listing["segmentation_defaults"] == BlockChunkerConfig().model_dump()
        assert listing["segmentation_version"] == SEGMENTATION_VERSION
    index = Index(db)
    state = finish(db, settings, values, state, Encoder(db, settings), index)
    parent = db.get(ChunkSet, state["chunk_set_id"])
    manifest = json.loads(values[3].data[parent.manifest_object_key])
    manifest_rows = {item["chunk_id"]: item for item in manifest["chunks"]}
    indexed_rows = {item["chunk_id"]: item for item in index.calls[-1]}
    tables = [c for c in service._chunks(db, state["chunk_set_id"]) if "table" in c.source_metadata["block_types"]]
    assert tables
    for chunk in tables:
        public = DocumentChunkRead.from_chunk(chunk)
        assert public.content_format == chunk.content_format and public.chunk_method == chunk.chunk_method
        assert public.source_metadata["table_fragmented"] is (not intact)
        assert manifest_rows[str(chunk.id)]["source_metadata"] == chunk.source_metadata
        assert indexed_rows[str(chunk.id)]["source_metadata"] == chunk.source_metadata
        assert chunk.content == frozen.canonical.decode()[chunk.source_start:chunk.source_end]


@pytest.mark.parametrize("length", [254, 255, 256, 300])
def test_s3_section_title_projection_manifest_sql_index_and_recomputation(db, settings, length):
    from app.ingestion.frozen_source import json_bytes, sha256_bytes
    from app.ingestion.chunk_anchors import anchor_intervals, refs_for_interval
    from test_versioned_index import Client

    title = ("铸😀é𝄞" * 75)[:length]
    path = ["完整父章节" * 60, title]
    expected_title = title[:255]
    values, frozen = structured_ready(db, settings, section_path=path)
    doc, version, build, assets, *_ = values
    source = kg._snapshot(db.get(SourceDocumentVersion, version))
    original_assets = {key: assets.data[key] for key in (source["canonical_object_key"], source["block_map_object_key"])}
    db.commit()
    state = prepare(db, settings, values, BlockChunkerConfig())
    row = kg._snapshot(db.get(ChunkSet, state["chunk_set_id"]))
    db.commit()
    anchors = kg.load_anchor_index(db, doc, version, build)
    drafts, manifest = service._drafts(source, row, anchors, assets, settings)
    again, same_manifest = service._drafts(source, row, anchors, assets, settings)
    assert drafts == again and json_bytes(manifest) == json_bytes(same_manifest)
    digest = sha256_bytes(json_bytes(manifest))
    assert manifest["schema_version"] == 3
    assert manifest["canonical_sha256"] == frozen.canonical_sha256
    assert manifest["source_map_sha256"] == frozen.block_map_sha256
    intervals = anchor_intervals(anchors, document_id=doc, source_version=version,
        graph_build_id=build, character_count=frozen.character_count)
    selected = [d for d in drafts if d.source_metadata["section_path"] == path]
    assert selected and any(d.source_metadata["kg_refs"] for d in selected)
    for draft in drafts:
        full_path = draft.source_metadata["section_path"]
        assert draft.section_title == (full_path[-1][:255] if full_path else None)
        assert draft.content == frozen.canonical.decode("utf-8")[draft.source_start:draft.source_end]
        assert draft.content_sha256 == sha256_bytes(draft.content.encode("utf-8"))
        assert draft.source_metadata["kg_refs"] == refs_for_interval(draft.source_start, draft.source_end, intervals)
    assert all(d.section_title == expected_title for d in selected)

    client = Client(normalize=True)
    gateway, encoder = VersionedIndex(settings, client), Encoder(db, settings)
    # Exercise explicit recovery for a long projected title as well as normal resume.
    if length == 300:
        client.partial = True
        with pytest.raises(BusinessError) as caught:
            finish(db, settings, values, state, encoder, gateway)
        assert caught.value.code == "CHUNK_INDEXING_FAILED"
        calls = len(encoder.calls)
        client.partial = False
        state = service.advance_chunk_set(db, doc, state["chunk_set_id"], retry=True,
            settings=settings, assets=assets, provider=encoder, index=gateway)
        assert len(encoder.calls) == calls
    else:
        state = finish(db, settings, values, state, encoder, gateway)
    parent = db.get(ChunkSet, state["chunk_set_id"])
    assert parent.status == "indexed" and client.blocked
    assert parent.manifest_sha256 == digest and assets.data[parent.manifest_object_key] == json_bytes(manifest)
    rows = [chunk_snapshot(c) for c in service._chunks(db, parent.id)]
    service._verify_chunks(rows, drafts, manifest)
    service._verify_links(db, parent.id, manifest)
    for stored, entry in zip(rows, manifest["chunks"], strict=True):
        payload = client.rows[str(stored["id"])]
        assert str(stored["id"]) == entry["chunk_id"]
        assert stored["section_title"] == entry["section_title"] == payload["section_title"]
        assert stored["source_metadata"] == entry["source_metadata"] == payload["source_metadata"]
    assert all(assets.data[key] == raw for key, raw in original_assets.items())
    db.commit()
    rebuilt, rebuilt_manifest = service._drafts(source, row, anchors, assets, settings)
    assert rebuilt == drafts and sha256_bytes(json_bytes(rebuilt_manifest)) == digest

    # Full comparison remains strict even when a corrupted display title fits VARCHAR(255).
    corrupt_rows = deepcopy(rows)
    corrupt_rows[0]["section_title"] = "tampered"
    with pytest.raises(ValueError, match="Chunk differs"):
        service._verify_chunks(corrupt_rows, drafts, manifest)
    intended = deepcopy(list(client.rows.values()))
    bad = deepcopy(intended[0]); bad["section_title"] = "tampered"
    factory = lambda: Session(db.get_bind())
    name = index_name(parent.id)
    hits = [SearchEngineHit(p["chunk_id"], 1, p, name) for p in intended + [bad]]
    kept, _ = filter_published_hits(hits, [], factory, settings, published_targets(factory, settings))
    assert len(kept) == len(intended)
    owner = {k: parent.index_receipt[k] for k in
        ("document_id", "source_version", "graph_build_id", "manifest_sha256", "embedding_fingerprint")}
    client.rows[intended[0]["chunk_id"]]["section_title"] = "tampered"
    with pytest.raises(ValueError, match="payload"):
        gateway.publish(parent.id, owner, intended)
