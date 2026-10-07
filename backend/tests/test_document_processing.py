"""M5 offline integration. Real ORM/SQLite, synthetic PDF/model/storage gateways.

Does not validate PostgreSQL triggers/concurrency or real external services.
"""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from app.core.errors import BusinessError
from app.ingestion.sequential_chunker import SegmentationConfig
from app.models import Document, DocumentChunk, DocumentProcessingJob, KGExtractionUnit
from app.models.document_chunk_set import ChunkSet
from app.services import document_processing as service
from app.services import document_graph_builds as kg
from app.services import document_chunk_sets as chunks
from app.services import document_parsing
from test_kg_v2_builds import db, settings, MemoryAssets, Provider, Writer
from test_chunk_set_builds import Encoder, Index
from test_document_parsing_mineru import _install_success_dependencies


def setup(db, settings, monkeypatch):
    settings.document_processing_executor_enabled = settings.pdf_kg_chunks_enabled = True
    settings.pdf_kg_embedding_revision = "synthetic-m5"
    settings.pdf_cleaning_enabled = True
    client, uploads = _install_success_dependencies(monkeypatch)
    item = Document(id=uuid4(), original_filename="m5.pdf", file_type=".pdf", mime_type="application/pdf",
        bucket_name="synthetic", object_key=f"raw/2026/10/{uuid4()}.pdf", process_status="uploaded")
    item.object_key = f"raw/2026/10/{item.id}.pdf"
    db.add(item)
    db.commit()
    doc = item.id
    result = service.request_processing(db, doc, uuid4(), SegmentationConfig(chunk_size=50, overlap=5), settings=settings)
    return doc, result, client, uploads


def step(db, settings, doc, job, **kwargs):
    return service.advance_processing(db, doc, job, settings=settings, worker_id="test-worker", **kwargs)


def finish(db, settings, doc, job, assets, **kwargs):
    stages = []
    for _ in range(80):
        current = service.processing_status(db, doc, job)
        stages.append(current["stage"])
        if current["status"] in {"succeeded", "failed", "cancelled"}:
            return current, stages
        step(db, settings, doc, job, assets=assets, **kwargs)
    pytest.fail("Pipeline did not reach a terminal status")


def parsed(db, settings, monkeypatch):
    doc, result, client, uploads = setup(db, settings, monkeypatch)
    step(db, settings, doc, result["job_id"])
    assets = MemoryAssets(db)
    assets.data.update({u["object_key"]: u["content"] for u in uploads})
    return doc, result["job_id"], assets, client


def test_full_pipeline_atomic_source_handoff_and_restart(db, settings, monkeypatch):
    doc, job, assets, client = parsed(db, settings, monkeypatch)
    state = service.processing_status(db, doc, job)
    assert state["stage"] == "source_ready" and state["source_version"] and state["parse_run_id"]
    assert not db.scalar(select(DocumentChunk.id))
    db.commit()
    model, writer, encoder, index = Provider(db), Writer(db), Encoder(db, settings), Index(db)
    state, stages = finish(db, settings, doc, job, assets, graph_provider=model, writer=writer, encoder=encoder, index=index)
    assert state["status"] == "succeeded" and state["stage"] == "indexed"
    assert stages.index("kg_ready") < stages.index("chunks_ready") < stages.index("indexing") < stages.index("indexed")
    assert len(client.requests) == 1 and len(writer.calls) == 1 and encoder.calls and index.calls
    assert db.scalar(select(func.count()).select_from(DocumentProcessingJob)) == 1
    assert db.get(Document, doc).current_chunk_set_id == state["chunk_set_id"]
    assert "pipeline" in db.get(DocumentProcessingJob, job).checkpoint
    assert not state["requires_io_reconciliation"]
    db.expire_all()  # Reload durable state as a new request/restarted worker does.
    assert service.processing_status(db, doc, job)["status"] == "succeeded"
    assert not step(db, settings, doc, job)


def test_request_idempotence_config_conflict_and_legacy_chunk_rejection(db, settings, monkeypatch):
    doc, result, *_ = setup(db, settings, monkeypatch)
    again = service.request_processing(db, doc, result["request_id"], SegmentationConfig(chunk_size=50, overlap=5), settings=settings)
    assert again["job_id"] == result["job_id"]
    with pytest.raises(BusinessError) as exc:
        service.request_processing(db, doc, result["request_id"], SegmentationConfig(chunk_size=51, overlap=5), settings=settings)
    assert exc.value.code == "DOCUMENT_PROCESSING_REQUEST_CONFLICT"
    db.rollback()
    with pytest.raises(BusinessError) as exc:
        document_parsing.parse_document(db, doc, settings=settings)
    assert exc.value.code == "DOCUMENT_PROCESSING_BUSY"
    db.rollback()
    service.cancel_processing(db, doc, result["job_id"])
    db.add(DocumentChunk(document_id=doc, chunk_index=0, content="legacy", source_metadata={}))
    db.commit()
    with pytest.raises(BusinessError) as exc:
        service.request_processing(db, doc, uuid4(), SegmentationConfig(), settings=settings)
    assert exc.value.code == "DOCUMENT_PROCESSING_EXISTING_CHUNKS"


def test_failure_blocks_downstream_and_explicit_retry_preserves_units(db, settings, monkeypatch):
    doc, job, assets, _ = parsed(db, settings, monkeypatch)
    step(db, settings, doc, job, assets=assets)
    failed = Provider(db, [RuntimeError("private provider URL token")])
    with pytest.raises(BusinessError):
        step(db, settings, doc, job, assets=assets, graph_provider=failed)
    state = service.processing_status(db, doc, job)
    assert state["status"] == "failed" and state["can_retry"]
    assert "private" not in str(state) and not db.scalar(select(DocumentChunk.id))
    db.commit()
    assert not step(db, settings, doc, job, assets=assets)
    service.retry_processing(db, doc, job, settings=settings)
    final, _ = finish(db, settings, doc, job, assets, graph_provider=Provider(db), writer=Writer(db),
        encoder=Encoder(db, settings), index=Index(db))
    assert final["status"] == "succeeded" and not final["requires_io_reconciliation"]


def test_owned_empty_index_retry_preserves_kg_embeddings_and_reconciliation_flag(db, settings, monkeypatch):
    from app.models import GraphBuild
    from app.search_engine.versioned_index import VersionedIndex
    from test_versioned_index import Client
    doc, job, assets, parser = parsed(db, settings, monkeypatch)
    model, writer, encoder = Provider(db), Writer(db), Encoder(db, settings)
    client = Client(normalize=True)
    gateway = VersionedIndex(settings, client)
    original = client.get_mapping
    def unavailable(**kwargs):
        raise TimeoutError("SECRET_RESPONSE_BODY")
    monkeypatch.setattr(client, "get_mapping", unavailable)
    with pytest.raises(BusinessError) as caught:
        finish(db, settings, doc, job, assets, graph_provider=model, writer=writer, encoder=encoder, index=gateway)
    assert caught.value.code == "CHUNK_INDEXING_FAILED"
    state = service.processing_status(db, doc, job)
    assert state["stage"] == "indexing" and state["requires_io_reconciliation"]
    assert client.mapping and not client.rows and client.calls == ["create"]
    build_id, set_id = state["graph_build_id"], state["chunk_set_id"]
    graph_before = deepcopy(kg._snapshot(db.get(GraphBuild, build_id)))
    units_before = deepcopy([kg._snapshot(u) for u in kg._units(db, build_id)])
    def saved_chunks():
        return [(c.id, c.content_sha256, c.source_start, c.source_end, list(c.embedding))
                for c in db.scalars(select(DocumentChunk).where(DocumentChunk.chunk_set_id == set_id).order_by(DocumentChunk.id))]
    chunks_before = saved_chunks()
    calls_before = (len(parser.requests), len(model.calls), len(writer.calls), len(encoder.calls))
    def forbidden(*args, **kwargs):
        pytest.fail("Index retry must not repeat parsing, KG or embedding")
    monkeypatch.setattr(document_parsing, "parse_document", forbidden)
    monkeypatch.setattr(model, "generate", forbidden)
    monkeypatch.setattr(writer, "write", forbidden)
    monkeypatch.setattr(encoder, "encode_documents", forbidden)
    monkeypatch.setattr(client, "get_mapping", original)
    service.retry_processing(db, doc, job, settings=settings)
    final, _ = finish(db, settings, doc, job, assets, graph_provider=model, writer=writer, encoder=encoder, index=gateway)
    assert final["status"] == "succeeded" and final["stage"] == "indexed"
    assert db.get(Document, doc).current_chunk_set_id == set_id
    assert db.get(Document, doc).publication_revision == 1
    assert db.get(ChunkSet, set_id).status == "indexed" and len(client.rows) == len(chunks_before)
    assert client.calls == ["create", "bulk", "seal"] and client.search_calls == 1
    assert saved_chunks() == chunks_before
    assert kg._snapshot(db.get(GraphBuild, build_id)) == graph_before
    assert [kg._snapshot(u) for u in kg._units(db, build_id)] == units_before
    assert (len(parser.requests), len(model.calls), len(writer.calls), len(encoder.calls)) == calls_before
    # Existing lifecycle retains uncertain-IO history even after successful retry.
    assert final["requires_io_reconciliation"] is True
    assert db.get(DocumentProcessingJob, job).checkpoint["external_write_pending"] is False


def test_managed_steps_cannot_be_started_by_manual_api(db, settings, monkeypatch):
    doc, job, assets, _ = parsed(db, settings, monkeypatch)
    step(db, settings, doc, job, assets=assets)
    state = service.processing_status(db, doc, job)
    with pytest.raises(BusinessError) as exc:
        kg.advance_graph_build(db, doc, state["graph_build_id"], settings=settings, assets=assets)
    assert exc.value.code == "DOCUMENT_PROCESSING_MANAGED_TASK"


def test_managed_retry_rejects_changed_kg_budget_before_external_work(db, settings, monkeypatch):
    settings.llm_max_tokens = settings.kg_llm_max_tokens = 1024
    doc, job, assets, _ = parsed(db, settings, monkeypatch)
    step(db, settings, doc, job, assets=assets)
    provider = Provider(db, [BusinessError("LLM_JSON_INVALID", "synthetic")])
    writer = Writer(db)
    with pytest.raises(BusinessError):
        step(db, settings, doc, job, assets=assets, graph_provider=provider, writer=writer)
    state = service.processing_status(db, doc, job)
    build = db.get(kg.GraphBuild, state["graph_build_id"])
    before = deepcopy([kg._snapshot(row) for row in [build, *kg._units(db, build.id)]])
    contract = deepcopy(db.get(DocumentProcessingJob, job).checkpoint["pipeline"])
    events = list(assets.events)
    settings.kg_llm_max_tokens = 8192
    # Existing retry queues work; the worker checks the frozen contract before any IO.
    service.retry_processing(db, doc, job, settings=settings)
    with pytest.raises(BusinessError) as caught:
        step(db, settings, doc, job, assets=assets, graph_provider=provider, writer=writer)
    assert caught.value.code == "DOCUMENT_PROCESSING_INPUT_CHANGED"
    assert service.processing_status(db, doc, job)["status"] == "failed"
    assert db.get(DocumentProcessingJob, job).checkpoint["pipeline"] == contract
    assert [kg._snapshot(row) for row in [build, *kg._units(db, build.id)]] == before
    assert len(provider.calls) == 1 and not writer.calls and assets.events == events
    assert not db.scalar(select(DocumentChunk.id)) and not db.scalar(select(ChunkSet.id))


def test_cancel_after_model_does_not_publish_or_start_chunks(db, settings, monkeypatch):
    doc, job, assets, _ = parsed(db, settings, monkeypatch)
    step(db, settings, doc, job, assets=assets)
    provider = Provider(db)
    provider.hook = lambda: service.cancel_processing(db, doc, job)
    step(db, settings, doc, job, assets=assets, graph_provider=provider)
    step(db, settings, doc, job, assets=assets)
    assert service.processing_status(db, doc, job)["status"] == "cancelled"
    assert not db.scalar(select(DocumentChunk.id))


def test_expired_parse_recovery_fences_late_result_and_blocks_deletion(db, settings, monkeypatch):
    doc, state, *_ = setup(db, settings, monkeypatch)
    leases = []
    def crashed(db, document_id, **kwargs):
        leases.append(kwargs["processing_lease"])
        raise KeyboardInterrupt("synthetic process crash")
    with pytest.raises(KeyboardInterrupt):
        step(db, settings, doc, state["job_id"], parser=crashed)
    job = db.get(DocumentProcessingJob, state["job_id"])
    job.lease_expires_at = kg._now(db) - timedelta(seconds=1)
    job.locked_at = kg._now(db) - timedelta(seconds=601)
    db.commit()
    service.retry_processing(db, doc, state["job_id"], settings=settings)
    with pytest.raises(BusinessError) as exc:
        leases[0].check(db)
    assert exc.value.code == "DOCUMENT_PROCESSING_LEASE_LOST"
    db.rollback()
    assert service.processing_status(db, doc, state["job_id"])["requires_io_reconciliation"]
    from app.services.document_version_deletion import check_processing_quiescent
    with pytest.raises(BusinessError) as exc:
        check_processing_quiescent(db, db.get(Document, doc))
    assert exc.value.code == "DOCUMENT_DELETION_IO_RECONCILIATION_REQUIRED"


def test_rechunk_worker_never_parses_extracts_or_writes_kg(db, settings, monkeypatch):
    doc, job, assets, _ = parsed(db, settings, monkeypatch)
    state, _ = finish(db, settings, doc, job, assets, graph_provider=Provider(db), writer=Writer(db), encoder=Encoder(db, settings), index=Index(db))
    identities = [(u.id, u.allocated_anchor_id) for u in db.scalars(select(KGExtractionUnit))]
    def forbidden(*args, **kwargs):
        pytest.fail("Rechunk invoked KG/parser")
    for name in ("prepare_graph_build", "advance_graph_build", "make_graph_id", "extract_piece", "configured_writer"):
        monkeypatch.setattr(kg, name, forbidden)
    monkeypatch.setattr(document_parsing, "parse_document", forbidden)
    for size, overlap, boundary in ((40, 0, "characters"), (60, 5, "line"), (80, 20, "paragraph")):
        result = chunks.prepare_chunk_set(db, doc, state["source_version"], state["graph_build_id"], uuid4(),
            SegmentationConfig(chunk_size=size, overlap=overlap, boundary=boundary), operation="rechunk", settings=settings)
        service.manage_rechunk(db, doc, result["job_id"], settings=settings)
        final, _ = finish(db, settings, doc, result["job_id"], assets, encoder=Encoder(db, settings), index=Index(db))
        assert final["status"] == "succeeded"
    assert identities == [(u.id, u.allocated_anchor_id) for u in db.scalars(select(KGExtractionUnit))]
    assert db.scalar(select(func.count()).select_from(ChunkSet)) == 4


def test_empty_kg_success_is_visible_and_continues_without_graph_write(db, settings, monkeypatch):
    doc, job, assets, _ = parsed(db, settings, monkeypatch)
    writer = Writer(db)
    state, _ = finish(db, settings, doc, job, assets,
        graph_provider=Provider(db, [{"entities": [], "relationships": []}] * 100), writer=writer,
        encoder=Encoder(db, settings), index=Index(db))
    assert state["status"] == "succeeded" and state["graph_status"] == "ready_empty"
    assert state["unit_count"] == state["completed_units"]
    assert state["chunk_count"] == state["embedded_count"] and not writer.calls
    assert all(not c.source_metadata["kg_refs"] for c in db.scalars(select(DocumentChunk)))


def test_cancel_during_index_write_never_publishes_pointer(db, settings, monkeypatch):
    doc, job, assets, _ = parsed(db, settings, monkeypatch)
    index = Index(db)
    index.hook = lambda: service.cancel_processing(db, doc, job)
    state, _ = finish(db, settings, doc, job, assets, graph_provider=Provider(db), writer=Writer(db),
        encoder=Encoder(db, settings), index=index)
    assert state["status"] == "cancelled" and state["chunk_set_status"] == "indexing"
    assert db.get(Document, doc).current_chunk_set_id is None and len(index.calls) == 1
    assert not state["requires_io_reconciliation"]


def test_ambiguous_external_write_failure_keeps_reconciliation_requirement(db, settings, monkeypatch):
    doc, job, assets, _ = parsed(db, settings, monkeypatch)
    writer = Writer(db)
    writer.fail = True
    with pytest.raises(BusinessError):
        finish(db, settings, doc, job, assets, graph_provider=Provider(db), writer=writer)
    assert service.processing_status(db, doc, job)["requires_io_reconciliation"]
    assert not db.scalar(select(DocumentChunk.id))


def test_changed_contract_and_corrupt_config_stop_queue_instead_of_looping(db, settings, monkeypatch):
    doc, state, *_ = setup(db, settings, monkeypatch)
    settings.pdf_cleaning_backfill_enabled = not settings.pdf_cleaning_backfill_enabled
    with pytest.raises(BusinessError) as exc:
        step(db, settings, doc, state["job_id"])
    assert exc.value.code == "DOCUMENT_PROCESSING_INPUT_CHANGED"
    assert service.processing_status(db, doc, state["job_id"])["status"] == "failed"


def test_existing_m2_handoff_can_be_explicitly_adopted_without_rebuilding(db, settings):
    from test_chunk_set_builds import ready
    values = ready(db, settings)
    doc, source, build, assets, request, model, writer = values
    settings.document_processing_executor_enabled = True
    job = db.scalar(select(DocumentProcessingJob))
    job_id, calls = job.id, (len(model.calls), len(writer.calls))
    db.commit()
    adopted = service.manage_rechunk(db, doc, job_id, settings=settings,
        config=SegmentationConfig(chunk_size=60, overlap=5))
    assert adopted["managed"] and adopted["request_id"] == request
    final, _ = finish(db, settings, doc, job_id, assets, graph_provider=model, writer=writer,
        encoder=Encoder(db, settings), index=Index(db))
    assert final["status"] == "succeeded" and (final["source_version"], final["graph_build_id"]) == (source, build)
    assert (len(model.calls), len(writer.calls)) == calls
