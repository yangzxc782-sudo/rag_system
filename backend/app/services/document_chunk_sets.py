"""Staged sequential retrieval builds. No parser or KG mutation calls.

Every external call uses snapshots after SQL commit. Publication is a short
Document-first fenced transaction; historical ChunkSets are never rewritten.
"""
from datetime import timedelta
from uuid import UUID, uuid4, uuid5
import json
import logging

from sqlalchemy import inspect, select, func
from sqlalchemy.orm import Session, undefer

from app.core.config import get_settings
from app.core.errors import BusinessError
from app.ingestion.frozen_source import json_bytes, sha256_bytes
from app.ingestion.sequential_chunker import SegmentationConfig, SEGMENTATION_VERSION, split_sequential
from app.models import Document, DocumentChunk, DocumentBlock, DocumentChunkBlock, SourceDocumentVersion, GraphBuild, DocumentProcessingJob
from app.models.document_chunk_set import ChunkSet
from app.retrieval.embeddings import get_embedding_provider
from app.search_engine.versioned_index import VersionedIndex, index_name, indexing_failure_details, payload_digest
from app.services.document_graph_builds import GraphAssets, _source_text, _snapshot, _now, _utc, load_anchor_index, require_graph_schema
from app.services.document_operation_guard import DocumentOperationGuard
from app.services.embedding_contract import embedding_fingerprint, vector32
from app.services.search_index import extract_exact_terms

logger = logging.getLogger(__name__)


def error(code, status=409):
    return BusinessError(code, "切片版本尚未完成，请读取状态并按诊断显式重试。", status_code=status)


def require_chunk_schema(db):
    require_graph_schema(db)
    inspector = inspect(db.get_bind())
    for model in (ChunkSet, DocumentChunk, Document):
        if not inspector.has_table(model.__tablename__) or not set(model.__table__.columns.keys()).issubset(
                c["name"] for c in inspector.get_columns(model.__tablename__)):
            raise error("CHUNK_SCHEMA_UNAVAILABLE", 503)


def _enabled(settings):
    if not settings.pdf_kg_chunks_enabled:
        raise error("CHUNK_BUILD_DISABLED", 503)
    try:
        return embedding_fingerprint(settings)
    except ValueError as exc:
        raise error("CHUNK_EMBEDDING_CONFIG_INVALID", 422) from exc


def _build(db, doc, source, build):
    row = db.scalar(select(GraphBuild).where(GraphBuild.id == build, GraphBuild.document_id == doc,
        GraphBuild.source_version == source, GraphBuild.graph_schema_version == 2,
        GraphBuild.status.in_(("ready", "ready_empty")), GraphBuild.sealed_at.is_not(None)))
    if row is None:
        raise error("CHUNK_GRAPH_NOT_READY")
    return row


def prepare_chunk_set(db: Session, document_id: UUID, source_version: UUID, graph_build_id: UUID,
                      request_id: UUID, config: SegmentationConfig, *, operation="process", settings=None, worker_id=None) -> dict:
    settings = settings or get_settings()
    fingerprint = _enabled(settings)
    require_chunk_schema(db)
    if operation not in {"process", "rechunk"}:
        raise error("CHUNK_OPERATION_INVALID", 422)
    document = DocumentOperationGuard(db).lock_normal(document_id)
    if (document.file_type or "").lower() != ".pdf":
        raise error("CHUNK_PDF_REQUIRED")
    _build(db, document_id, source_version, graph_build_id)
    job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == document_id,
        DocumentProcessingJob.operation == operation, DocumentProcessingJob.request_id == request_id).with_for_update())
    from app.services.document_processing import reject_managed_step
    if job and not job.chunk_set_id:
        reject_managed_step(job, worker_id)
    if job and job.chunk_set_id:
        existing = db.get(ChunkSet, job.chunk_set_id)
        if (existing.source_version != source_version or existing.graph_build_id != graph_build_id
                or existing.segmentation_config_sha256 != config.fingerprint or existing.embedding_fingerprint != fingerprint):
            raise error("CHUNK_REQUEST_CONFLICT")
        set_id = existing.id
        db.commit()
        return chunk_set_status(db, document_id, set_id, settings=settings)
    current = db.get(ChunkSet, document.current_chunk_set_id) if document.current_chunk_set_id else None
    if operation == "rechunk":
        if current:
            if current.status != "indexed" or (current.source_version, current.graph_build_id) != (source_version, graph_build_id):
                raise error("RECHUNK_PUBLISHED_SOURCE_REQUIRED")
        elif not db.scalar(select(DocumentProcessingJob.id).join(ChunkSet, DocumentProcessingJob.chunk_set_id == ChunkSet.id)
                .where(DocumentProcessingJob.document_id == document_id, DocumentProcessingJob.status == "failed",
                    ChunkSet.document_id == document_id, ChunkSet.source_version == source_version,
                    ChunkSet.graph_build_id == graph_build_id, ChunkSet.status == "failed")):
            # A new explicit set can replace a permanently failed FIRST attempt.
            # It cannot bypass the graph/process handoff or rebind that job.
            raise error("RECHUNK_PREVIOUS_ATTEMPT_REQUIRED")
        if job:
            raise error("CHUNK_REQUEST_CONFLICT")
    elif (not job or job.source_version != source_version or job.graph_build_id != graph_build_id
            or job.stage != "kg_ready" or job.status != "queued" or current):
        raise error("CHUNK_PROCESS_JOB_NOT_READY")
    active = list(db.scalars(select(DocumentProcessingJob.id).where(DocumentProcessingJob.document_id == document_id,
        DocumentProcessingJob.status.in_(("queued", "running", "retry_wait")))))
    if any(key != (job.id if job else None) for key in active):
        raise error("CHUNK_DOCUMENT_BUSY")
    row = ChunkSet(id=uuid4(), document_id=document_id, source_version=source_version, graph_build_id=graph_build_id,
        segmentation_version=SEGMENTATION_VERSION, segmentation_config=config.model_dump(),
        segmentation_config_sha256=config.fingerprint, embedding_fingerprint=fingerprint, status="pending")
    db.add(row)
    db.flush()
    if job is None:
        job = DocumentProcessingJob(document_id=document_id, operation=operation, request_id=request_id,
            input_fingerprint=sha256_bytes(json_bytes(dict(source=str(source_version), build=str(graph_build_id),
                config=config.fingerprint, embedding=fingerprint))), source_version=source_version, graph_build_id=graph_build_id,
            stage="kg_ready", status="queued", attempt_count=0)
        db.add(job)
    job.chunk_set_id = row.id
    job.checkpoint = {**(job.checkpoint or {}), "base_revision": document.publication_revision,
                      "base_chunk_set": str(document.current_chunk_set_id) if document.current_chunk_set_id else None}
    # One lease per bounded batch, plus explicit failure recovery attempts.
    job.max_attempts = job.attempt_count + settings.pdf_kg_max_chunks + 10
    set_id = row.id
    db.commit()
    return chunk_set_status(db, document_id, set_id, settings=settings)


def _locked(db, document_id, set_id):
    document = DocumentOperationGuard(db).lock_normal(document_id)
    job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == document_id,
        DocumentProcessingJob.chunk_set_id == set_id).execution_options(populate_existing=True).with_for_update())
    row = db.scalar(select(ChunkSet).where(ChunkSet.id == set_id, ChunkSet.document_id == document_id)
                    .execution_options(populate_existing=True).with_for_update())
    if row is None or job is None:
        raise error("CHUNK_SET_NOT_FOUND", 404)
    return document, job, row


def _fenced(db, doc, set_id, lease):
    document, job, row = _locked(db, doc, set_id)
    if (job.status != "running" or (job.lease_token, job.fencing_token) != lease
            or _utc(job.lease_expires_at) <= _now(db) or row.status == "indexed"):
        raise error("CHUNK_LEASE_LOST")
    _build(db, doc, row.source_version, row.graph_build_id)
    return document, job, row


def _release(job, status, now):
    from app.services.document_processing import finish_external_write
    finish_external_write(job, status)
    job.status = status
    job.locked_by = job.lease_token = job.locked_at = job.lease_expires_at = job.next_retry_at = None
    job.finished_at = now if status in {"succeeded", "failed", "cancelled"} else None


def chunk_set_status(db: Session, document_id: UUID, set_id: UUID, *, settings=None) -> dict:
    require_chunk_schema(db)
    document, job, row = _locked(db, document_id, set_id)
    counts = dict(db.execute(select(DocumentChunk.embedding_status, func.count()).where(DocumentChunk.chunk_set_id == set_id)
                            .group_by(DocumentChunk.embedding_status)).all())
    result = dict(chunk_set_id=row.id, document_id=document_id, source_version=row.source_version,
        graph_build_id=row.graph_build_id, request_id=job.request_id, operation=job.operation, status=row.status,
        job_status=job.status, stage=job.stage, chunk_count=row.chunk_count, embedding_counts=counts,
        segmentation_config=row.segmentation_config, is_current=document.current_chunk_set_id == row.id,
        publication_revision=document.publication_revision, index_name=row.index_name,
        last_error_code=job.last_error_code, lease_expires_at=job.lease_expires_at,
        job_id=job.id, managed="pipeline" in job.checkpoint,
        can_advance="pipeline" not in job.checkpoint and row.status != "indexed" and job.attempt_count < job.max_attempts and (
            job.status in {"queued", "failed"} or job.status == "running" and _utc(job.lease_expires_at) <= _now(db)),
        search_enabled=bool((settings or get_settings()).pdf_kg_search_enabled))
    db.commit()
    return result


def list_chunk_sets(db: Session, document_id: UUID, *, limit=20, offset=0) -> dict:
    require_chunk_schema(db)
    document = DocumentOperationGuard(db).lock_normal(document_id)
    current_id = document.current_chunk_set_id
    ids = list(db.scalars(select(ChunkSet.id).where(ChunkSet.document_id == document_id)
        .order_by(ChunkSet.created_at.desc(), ChunkSet.id).limit(limit).offset(offset)))
    total = db.scalar(select(func.count()).select_from(ChunkSet).where(ChunkSet.document_id == document_id))
    job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == document_id,
        DocumentProcessingJob.operation == "process", DocumentProcessingJob.stage == "kg_ready",
        DocumentProcessingJob.status == "queued", DocumentProcessingJob.chunk_set_id.is_(None)))
    initial = dict(source_version=job.source_version, graph_build_id=job.graph_build_id, request_id=job.request_id) if job and "pipeline" not in job.checkpoint else None
    db.commit()
    items = [chunk_set_status(db, document_id, key) for key in ids]
    current = next((item for item in items if item["chunk_set_id"] == current_id), None)
    if current_id and current is None:
        current = chunk_set_status(db, document_id, current_id)
    return dict(items=items, current=current, process_ready=initial, total=total, limit=limit, offset=offset)


def _chunks(db, set_id):
    return list(db.scalars(select(DocumentChunk).options(undefer("*")).where(DocumentChunk.chunk_set_id == set_id)
                           .order_by(DocumentChunk.chunk_index)))


def _drafts(source, row, anchors, assets, settings):
    text, blocks = _source_text(source, assets)
    config = SegmentationConfig.model_validate(row["segmentation_config"])
    if row["segmentation_version"] != SEGMENTATION_VERSION or config.fingerprint != row["segmentation_config_sha256"]:
        raise ValueError("Unknown or corrupt segmentation contract")
    drafts = split_sequential(text, config, document_id=row["document_id"], source_version=row["source_version"],
        graph_build_id=row["graph_build_id"], anchors=anchors, blocks=blocks, max_chunks=settings.pdf_kg_max_chunks)
    entries = [dict(chunk_id=str(uuid5(row["id"], str(i))), chunk_index=i, source_start=d.source_start,
        source_end=d.source_end, content_sha256=d.content_sha256, kg_refs=list(d.kg_refs), block_ids=list(d.block_ids),
        page_start=d.page_start, page_end=d.page_end) for i, d in enumerate(drafts)]
    manifest = dict(schema_version=2, chunk_set_id=str(row["id"]), source_version=str(row["source_version"]),
        graph_build_id=str(row["graph_build_id"]), canonical_sha256=source["canonical_sha256"],
        segmentation_config_sha256=row["segmentation_config_sha256"], chunks=entries)
    return drafts, manifest


def _verify_chunks(rows, drafts, manifest):
    if len(rows) != len(drafts):
        raise ValueError("Chunk manifest count mismatch")
    for row, draft, entry in zip(rows, drafts, manifest["chunks"], strict=True):
        if (str(row["id"]) != entry["chunk_id"] or row["chunk_index"] != entry["chunk_index"]
                or row["content"] != draft.content or row["content_sha256"] != draft.content_sha256
                or row["source_start"] != draft.source_start or row["source_end"] != draft.source_end
                or row["source_metadata"] != {"kg_refs": list(draft.kg_refs)}
                or row["page_start"] != draft.page_start or row["page_end"] != draft.page_end
                or str(row["chunk_set_id"]) != manifest["chunk_set_id"] or str(row["source_version"]) != manifest["source_version"]):
            raise ValueError("Chunk differs from frozen source/anchors")


def advance_chunk_set(db: Session, document_id: UUID, set_id: UUID, *, retry=False, settings=None, assets=None, provider=None, index=None, worker_id=None) -> dict:
    settings, assets = settings or get_settings(), assets or GraphAssets()
    fingerprint = _enabled(settings)
    require_chunk_schema(db)
    document, job, row = _locked(db, document_id, set_id)
    from app.services.document_processing import reject_managed_step
    reject_managed_step(job, worker_id)
    if row.status == "indexed":
        db.commit()
        return chunk_set_status(db, document_id, set_id, settings=settings)
    if row.embedding_fingerprint != fingerprint:
        raise error("CHUNK_EMBEDDING_IDENTITY_CHANGED")
    now = _now(db)
    if job.status == "running" and _utc(job.lease_expires_at) > now:
        raise error("CHUNK_JOB_BUSY")
    if job.status in {"running", "failed"} and not retry:
        raise error("CHUNK_EXPLICIT_RETRY_REQUIRED")
    if job.status not in {"queued", "running", "failed"} or job.attempt_count >= job.max_attempts:
        raise error("CHUNK_JOB_NOT_CLAIMABLE")
    if db.scalar(select(DocumentProcessingJob.id).where(DocumentProcessingJob.document_id == document_id,
            DocumentProcessingJob.id != job.id, DocumentProcessingJob.status.in_(("queued", "running", "retry_wait")))):
        raise error("CHUNK_DOCUMENT_BUSY")
    _build(db, document_id, row.source_version, row.graph_build_id)
    source = _snapshot(db.get(SourceDocumentVersion, row.source_version))
    row_data, filename = _snapshot(row), document.original_filename
    if job.status == "running":
        job.checkpoint = {**job.checkpoint, "requires_io_reconciliation": True}
    job.status, job.locked_by, job.lease_token = "running", worker_id or "chunk-set-api", uuid4()
    job.fencing_token += 1
    job.attempt_count += 1
    job.locked_at, job.lease_expires_at = now, now + timedelta(seconds=settings.kg_lease_seconds)
    job.finished_at = job.next_retry_at = job.last_error_code = row.last_error_code = None
    lease = job.lease_token, job.fencing_token
    stage = "chunking" if row.sealed_at is None else ("indexing" if job.stage == "indexing" else "embedding")
    job.stage = row.status = stage
    document.process_status = stage
    db.commit()
    operation = "validate_source"
    try:
        anchors = load_anchor_index(db, document_id, row_data["source_version"], row_data["graph_build_id"])
        drafts, manifest = _drafts(source, row_data, anchors, assets, settings)
        if stage == "chunking":
            _, job, _ = _fenced(db, document_id, set_id, lease)
            from app.services.document_processing import begin_external_write
            begin_external_write(job)
            db.commit()
            key, digest = assets.save(source["bucket_name"], f"documents/{document_id}/chunk-sets/{set_id}/manifest", manifest)
            document, job, row = _fenced(db, document_id, set_id, lease)
            if _chunks(db, set_id):
                raise ValueError("Unsealed set already has chunks")
            required_blocks = {UUID(b) for d in drafts for b in d.block_ids}
            owned_blocks = set(db.scalars(select(DocumentBlock.id).where(DocumentBlock.id.in_(required_blocks),
                DocumentBlock.document_id == document_id, DocumentBlock.parse_run_id == source["parse_run_id"])))
            if owned_blocks != required_blocks:
                raise ValueError("Frozen blocks missing or belong to another source")
            for i, draft in enumerate(drafts):
                chunk_id = uuid5(set_id, str(i))
                db.add(DocumentChunk(id=chunk_id, document_id=document_id, parse_run_id=source["parse_run_id"],
                    chunk_set_id=set_id, source_version=row.source_version, chunk_index=i,
                    source_start=draft.source_start, source_end=draft.source_end, content=draft.content,
                    content_sha256=draft.content_sha256, source_metadata={"kg_refs": list(draft.kg_refs)},
                    page_start=draft.page_start, page_end=draft.page_end, chunk_type="text", chunk_method="sequential",
                    content_format="markdown", embedding_status="not_started"))
                db.flush()
                for order, block_id in enumerate(draft.block_ids):
                    db.add(DocumentChunkBlock(chunk_id=chunk_id, block_id=UUID(block_id), block_order=order))
            db.flush()  # All immutable content precedes sealing the parent.
            row.chunk_count, row.manifest_object_key, row.manifest_sha256, row.sealed_at = len(drafts), key, digest, _now(db)
            row.status = job.stage = document.process_status = "chunks_ready"
            _release(job, "queued", _now(db))
            db.commit()
        else:
            operation = "validate_manifest"
            saved = assets.read(source["bucket_name"], row_data["manifest_object_key"], row_data["manifest_sha256"])
            if saved != json_bytes(manifest):
                raise ValueError("Sealed manifest mismatch")
            _fenced(db, document_id, set_id, lease)
            rows = [_snapshot(c) for c in _chunks(db, set_id)]
            _verify_chunks(rows, drafts, manifest)
            db.commit()
            if stage == "embedding":
                _embed(db, document_id, set_id, lease, rows, row_data, settings, provider)
            else:
                operation = "prepare_payload"
                payloads = [_payload(c, row_data, filename, settings) for c in rows]
                _, job, _ = _fenced(db, document_id, set_id, lease)
                from app.services.document_processing import begin_external_write
                begin_external_write(job)
                db.commit()
                owner = dict(document_id=str(document_id), source_version=str(row_data["source_version"]),
                    graph_build_id=str(row_data["graph_build_id"]), manifest_sha256=row_data["manifest_sha256"],
                    embedding_fingerprint=fingerprint)
                operation = "index_client"
                gateway = index or VersionedIndex(settings)
                try:
                    operation = "index_publish"
                    receipt = gateway.publish(set_id, owner, payloads)
                finally:
                    if index is None:
                        operation = "index_close"
                        gateway.close()
                        operation = "index_publish"
                operation = "validate_receipt"
                expected = {**owner, "schema_version": 2, "chunk_set_id": str(set_id), "chunk_count": len(rows),
                    "index_name": index_name(set_id), "payload_sha256": payload_digest(payloads), "verified": True, "write_blocked": True}
                if receipt != expected:
                    raise ValueError("Index receipt mismatch")
                operation = "publication_guard"
                document, job, row = _fenced(db, document_id, set_id, lease)
                if job.checkpoint.get("cancel_requested"):
                    # The sealed external index remains an unadmitted asset.
                    _release(job, "cancelled", _now(db))
                    db.commit()
                    return chunk_set_status(db, document_id, set_id, settings=settings)
                base = job.checkpoint
                if (document.original_filename != filename or document.publication_revision != base["base_revision"] or
                        (str(document.current_chunk_set_id) if document.current_chunk_set_id else None) != base["base_chunk_set"]):
                    raise error("CHUNK_PUBLICATION_CONFLICT")
                # Recheck all vectors/content under the publication lock as well.
                fresh = [_payload(_snapshot(c), row_data, filename, settings) for c in _chunks(db, set_id)]
                if payload_digest(fresh) != receipt["payload_sha256"]:
                    raise ValueError("Chunks changed during index publication")
                operation = "sql_publish"
                row.status, row.index_name, row.index_receipt, row.indexed_at = "indexed", receipt["index_name"], receipt, _now(db)
                db.flush()  # Pointer trigger must see the ready set.
                document.current_chunk_set_id = set_id
                document.publication_revision += 1
                document.process_status = "retrieval_indexed"
                job.stage = "indexed"
                _release(job, "succeeded", _now(db))
                db.commit()
                logger.info("Chunk set published: document=%s set=%s", document_id, set_id)
    except Exception as exc:
        db.rollback()
        code = exc.code if isinstance(exc, BusinessError) else f"CHUNK_{stage.upper()}_FAILED"
        if stage == "indexing":
            diagnostics = dict(document_id=str(document_id), chunk_set_id=str(set_id), stage=stage,
                operation=operation, index_name=index_name(set_id), **indexing_failure_details(exc))
            log_code = diagnostics["cause_code"] if isinstance(exc, BusinessError) else code
            logger.warning("Chunk set step failed: document=%s set=%s stage=%s code=%s indexing_diagnostics=%s",
                document_id, set_id, stage, log_code, json.dumps(diagnostics, ensure_ascii=False),
                extra={"event": "chunk_set_indexing_failed", **diagnostics})
        else:
            logger.warning("Chunk set step failed: document=%s set=%s stage=%s code=%s", document_id, set_id, stage, code)
        try:
            document, job, row = _fenced(db, document_id, set_id, lease)
            row.status, row.last_error_code, job.last_error_code = "failed", code, code
            document.process_status = "retrieval_failed"
            _release(job, "failed", _now(db))
            db.commit()
        except Exception:
            db.rollback()  # A late worker may never overwrite a new lease/deletion state.
        raise error(code) from exc
    return chunk_set_status(db, document_id, set_id, settings=settings)


def _embed(db, doc, set_id, lease, rows, row_data, settings, provider):
    targets = [c for c in rows if c["embedding_status"] != "embedded"][:max(1, settings.embedding_batch_size)]
    reused = {}
    if targets:
        candidates = db.execute(select(DocumentChunk.content, DocumentChunk.embedding).join(ChunkSet, DocumentChunk.chunk_set_id == ChunkSet.id)
            .where(ChunkSet.document_id == doc, ChunkSet.source_version == row_data["source_version"], ChunkSet.status == "indexed",
                ChunkSet.embedding_fingerprint == row_data["embedding_fingerprint"],
                DocumentChunk.content_sha256.in_([c["content_sha256"] for c in targets]), DocumentChunk.embedding_status == "embedded"))
        for content, embedding in candidates:
            reused[content] = vector32(embedding, settings.embedding_dim)
        db.commit()
    missing = list(dict.fromkeys(c["content"] for c in targets if c["content"] not in reused))
    if missing:
        result = (provider or get_embedding_provider(settings)).encode_documents(missing)
        if (result.embedding_model != settings.embedding_model or result.embedding_dim != settings.embedding_dim
                or len(result.embeddings) != len(missing)):
            raise ValueError("Embedding result identity mismatch")
        reused.update({text: vector32(vector, settings.embedding_dim) for text, vector in zip(missing, result.embeddings, strict=True)})
    document, job, row = _fenced(db, doc, set_id, lease)
    for target in targets:
        chunk = db.get(DocumentChunk, target["id"])
        chunk.embedding = reused[target["content"]]
        chunk.embedding_model, chunk.embedding_dim, chunk.embedding_status = settings.embedding_model, settings.embedding_dim, "embedded"
        chunk.embedding_updated_at = _now(db)
        chunk.embedding_error_message = None
    db.flush()
    remaining = db.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.chunk_set_id == set_id,
        DocumentChunk.embedding_status != "embedded"))
    row.status = job.stage = document.process_status = "embedding" if remaining else "indexing"
    _release(job, "queued", _now(db))
    db.commit()


def _payload(c, row, filename, settings):
    if c["embedding_status"] != "embedded" or c["embedding_model"] != settings.embedding_model or c["embedding_dim"] != settings.embedding_dim:
        raise ValueError("Unready embedding")
    return dict(schema_version=2, chunk_id=str(c["id"]), document_id=str(c["document_id"]), original_filename=filename,
        chunk_index=c["chunk_index"], content=c["content"], content_max=c["content"], content_smart=c["content"],
        chunk_type=c["chunk_type"], page_start=c["page_start"], page_end=c["page_end"], section_title=c["section_title"],
        source_metadata=c["source_metadata"], exact_terms=extract_exact_terms(c["content"]),
        source_version=str(c["source_version"]), graph_build_id=str(row["graph_build_id"]), chunk_set_id=str(row["id"]),
        source_start=c["source_start"], source_end=c["source_end"], content_sha256=c["content_sha256"],
        embedding_fingerprint=row["embedding_fingerprint"], embedding=vector32(c["embedding"], settings.embedding_dim),
        embedding_model=c["embedding_model"], embedding_dim=c["embedding_dim"], embedding_status="embedded")
