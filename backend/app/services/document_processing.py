"""Durable PDF orchestration. External work is delegated to fenced M1–M3 stages.

Only queued, explicitly managed jobs are automatically advanced. Failed/expired
work requires an explicit retry; request status reads never schedule side effects.
"""
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.ingestion.frozen_source import json_bytes, sha256_bytes
from app.ingestion.block_chunker import BlockChunkerConfig, SEGMENTATION_VERSION, frozen_config
from app.models import Document, DocumentChunk, DocumentParseRun, DocumentProcessingJob, SourceDocumentVersion
from app.models.document_chunk_set import ChunkSet
from app.services.document_operation_guard import DocumentOperationGuard
from app.services.document_graph_builds import _now, _utc, require_graph_schema, provider_fingerprint
from app.services.embedding_contract import embedding_fingerprint

ACTIVE = {"queued", "running", "retry_wait"}
TERMINAL = {"succeeded", "cancelled"}


def error(code: str, status: int = 409) -> BusinessError:
    return BusinessError(code, "处理任务未就绪，请刷新状态并按诊断重试。", status_code=status)


def pipeline_contract(settings, config, *, operation="process"):
    config = BlockChunkerConfig.model_validate(config)
    # Secrets never enter the durable checkpoint or public diagnostics.
    parser = {key: getattr(settings, key) for key in (
        "document_parser_provider", "mineru_parse_mode", "mineru_enable_ocr", "mineru_save_intermediate",
        "mineru_output_prefix", "pdf_cleaning_enabled", "pdf_cleaning_profile", "pdf_cleaning_backfill_enabled")}
    try:
        embedding = embedding_fingerprint(settings)
    except ValueError as exc:
        raise error("DOCUMENT_PROCESSING_EMBEDDING_CONFIG_INVALID", 422) from exc
    return dict(version=1, config=config.model_dump(), segmentation_version=SEGMENTATION_VERSION,
                segmentation_config_sha256=config.fingerprint,
                parser_sha256=sha256_bytes(json_bytes(parser)) if operation == "process" else None,
                provider_sha256=provider_fingerprint(settings) if operation == "process" else None,
                embedding_sha256=embedding)


def _enabled(settings, *, operation="process"):
    if not settings.document_processing_executor_enabled:
        raise error("DOCUMENT_PROCESSING_EXECUTOR_DISABLED", 503)
    if not settings.pdf_kg_chunks_enabled or operation == "process" and (not settings.kg_build_enabled or not settings.pdf_cleaning_enabled):
        raise error("DOCUMENT_PROCESSING_CONFIG_INVALID", 503)


def _frozen_pipeline_config(pipeline):
    try:
        return frozen_config(pipeline["config"], pipeline["segmentation_config_sha256"], pipeline["segmentation_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise error("DOCUMENT_PROCESSING_INPUT_CHANGED") from exc


def _locked(db: Session, doc_id: UUID, job_id: UUID) -> tuple[Document, DocumentProcessingJob]:
    document = DocumentOperationGuard(db).lock_normal(doc_id)
    job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.id == job_id,
        DocumentProcessingJob.document_id == doc_id).execution_options(populate_existing=True).with_for_update())
    if job is None:
        raise error("DOCUMENT_PROCESSING_NOT_FOUND", 404)
    return document, job


def _release(job, status, now):
    finish_external_write(job, status)
    job.status = status
    job.locked_by = job.lease_token = job.locked_at = job.lease_expires_at = job.next_retry_at = None
    job.finished_at = now if status in {"failed", "succeeded", "cancelled"} else None


def begin_external_write(job):
    job.checkpoint = {**job.checkpoint, "external_write_pending": True}


def finish_external_write(job, status):
    if job.checkpoint.get("external_write_pending"):
        job.checkpoint = {**job.checkpoint, "external_write_pending": False,
            **({"requires_io_reconciliation": True} if status == "failed" else {})}


def public_job(job, now):
    checkpoint = job.checkpoint or {}
    expired = job.status == "running" and _utc(job.lease_expires_at) <= now
    return dict(job_id=job.id, document_id=job.document_id, request_id=job.request_id,
        operation=job.operation, status=job.status, stage=job.stage, source_version=job.source_version,
        graph_build_id=job.graph_build_id, chunk_set_id=job.chunk_set_id,
        parse_run_id=checkpoint.get("parse_run_id"), attempt_count=job.attempt_count, max_attempts=job.max_attempts,
        lease_expires_at=job.lease_expires_at, last_error_code=job.last_error_code,
        can_retry=(job.status == "failed" or expired) and job.attempt_count < job.max_attempts,
        can_cancel=job.status not in TERMINAL, cancel_requested=bool(checkpoint.get("cancel_requested")),
        requires_io_reconciliation=bool(checkpoint.get("requires_io_reconciliation")),
        managed="pipeline" in checkpoint, config=checkpoint.get("pipeline", {}).get("config"),
        created_at=job.created_at, updated_at=job.updated_at)


def job_progress(db, job):
    from app.models import GraphBuild, KGExtractionUnit
    build = db.get(GraphBuild, job.graph_build_id) if job.graph_build_id else None
    chunk_set = db.get(ChunkSet, job.chunk_set_id) if job.chunk_set_id else None
    completed = db.scalar(select(func.count()).select_from(KGExtractionUnit).where(
        KGExtractionUnit.graph_build_id == job.graph_build_id,
        KGExtractionUnit.status.in_(("succeeded_empty", "succeeded_nonempty")))) if build else 0
    embedded = db.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.chunk_set_id == job.chunk_set_id,
        DocumentChunk.embedding_status == "embedded")) if chunk_set else 0
    return dict(graph_status=build.status if build else None, unit_count=build.unit_count if build else None,
        completed_units=completed, chunk_count=chunk_set.chunk_count if chunk_set else None,
        embedded_count=embedded, chunk_set_status=chunk_set.status if chunk_set else None)


def processing_status(db: Session, document_id: UUID, job_id: UUID) -> dict:
    require_graph_schema(db)
    _, job = _locked(db, document_id, job_id)
    result = {**public_job(job, _now(db)), **job_progress(db, job)}
    db.commit()
    return result


def list_processing_jobs(db: Session, document_id: UUID, *, settings, limit: int = 20) -> dict:
    require_graph_schema(db)
    document = DocumentOperationGuard(db).lock_normal(document_id)
    jobs = list(db.scalars(select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == document_id)
        .order_by(DocumentProcessingJob.created_at.desc(), DocumentProcessingJob.id.desc()).limit(limit)))
    active = db.scalar(select(DocumentProcessingJob.id).where(DocumentProcessingJob.document_id == document_id,
        DocumentProcessingJob.status.in_(ACTIVE)).limit(1))
    has_chunks = db.scalar(select(DocumentChunk.id).where(DocumentChunk.document_id == document_id).limit(1))
    result = dict(segmentation_defaults=BlockChunkerConfig().model_dump(), segmentation_version=SEGMENTATION_VERSION,
        items=[{**public_job(j, _now(db)), **job_progress(db, j)} for j in jobs],
        total=db.scalar(select(func.count()).select_from(DocumentProcessingJob).where(DocumentProcessingJob.document_id == document_id)),
        executor_enabled=settings.document_processing_executor_enabled, search_enabled=settings.pdf_kg_search_enabled,
        can_process=bool(settings.document_processing_executor_enabled and not active and not has_chunks
            and (document.file_type or "").lower() == ".pdf" and document.process_status in {"uploaded", "parse_failed", "cleaned_source_ready"}))
    db.commit()
    return result


def request_processing(db: Session, document_id: UUID, request_id: UUID, config: BlockChunkerConfig, *, settings) -> dict:
    _enabled(settings)
    require_graph_schema(db)
    contract = pipeline_contract(settings, config)
    fingerprint = sha256_bytes(json_bytes(contract))
    document = DocumentOperationGuard(db).lock_normal(document_id)
    existing = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == document_id,
        DocumentProcessingJob.operation == "process", DocumentProcessingJob.request_id == request_id))
    if existing:
        if existing.input_fingerprint != fingerprint or existing.checkpoint.get("pipeline") != contract:
            raise error("DOCUMENT_PROCESSING_REQUEST_CONFLICT")
        result = {**public_job(existing, _now(db)), **job_progress(db, existing)}
        db.commit()
        return result
    if (document.file_type or "").lower() != ".pdf":
        raise error("DOCUMENT_PROCESSING_PDF_REQUIRED", 415)
    if db.scalar(select(DocumentProcessingJob.id).where(DocumentProcessingJob.document_id == document_id,
            DocumentProcessingJob.status.in_(ACTIVE))):
        raise error("DOCUMENT_PROCESSING_BUSY")
    if db.scalar(select(DocumentChunk.id).where(DocumentChunk.document_id == document_id).limit(1)):
        raise error("DOCUMENT_PROCESSING_EXISTING_CHUNKS")
    if document.process_status not in {"uploaded", "parse_failed", "cleaned_source_ready"}:
        raise error("DOCUMENT_PROCESSING_SOURCE_NOT_READY")
    sources = list(db.scalars(select(SourceDocumentVersion).where(SourceDocumentVersion.document_id == document_id)))
    if len(sources) > 1 or bool(sources) != (document.process_status == "cleaned_source_ready"):
        raise error("DOCUMENT_PROCESSING_SOURCE_CONFLICT")
    job = DocumentProcessingJob(id=uuid4(), document_id=document_id, operation="process", request_id=request_id,
        input_fingerprint=fingerprint, source_version=sources[0].source_version if sources else None,
        stage="source_ready" if sources else "uploaded", status="queued", checkpoint={"pipeline": contract}, max_attempts=4)
    db.add(job)
    db.commit()
    return processing_status(db, document_id, job.id)


def manage_rechunk(db: Session, document_id: UUID, job_id: UUID, *, settings, config: BlockChunkerConfig | None = None) -> dict:
    """Explicitly opt an existing M2/M3 task into background execution."""
    _, job = _locked(db, document_id, job_id)
    _enabled(settings, operation=job.operation)
    if job.status not in {"queued", "failed"} or not job.graph_build_id:
        raise error("DOCUMENT_PROCESSING_NOT_QUEUED")
    row = db.get(ChunkSet, job.chunk_set_id) if job.chunk_set_id else None
    saved = job.checkpoint.get("pipeline")
    frozen = (frozen_config(row.segmentation_config, row.segmentation_config_sha256, row.segmentation_version)
        if row else _frozen_pipeline_config(saved)
        if saved else None)
    if config is not None:
        config = BlockChunkerConfig.model_validate(config)
        if frozen is not None and config != frozen:
            raise error("DOCUMENT_PROCESSING_REQUEST_CONFLICT")
    config = frozen if frozen is not None else config if config is not None else BlockChunkerConfig()
    contract = pipeline_contract(settings, config, operation=job.operation)
    if row and row.embedding_fingerprint != contract["embedding_sha256"]:
        raise error("DOCUMENT_PROCESSING_INPUT_CHANGED")
    if not row:
        from app.models import GraphBuild
        graph = db.get(GraphBuild, job.graph_build_id)
        if graph is None or graph.provider_fingerprint != contract["provider_sha256"]:
            raise error("DOCUMENT_PROCESSING_INPUT_CHANGED")
    if "pipeline" not in job.checkpoint:
        job.checkpoint = {**job.checkpoint, "pipeline": contract}
    elif job.checkpoint["pipeline"] != contract:
        raise error("DOCUMENT_PROCESSING_REQUEST_CONFLICT")
    db.commit()
    return processing_status(db, document_id, job_id)


def retry_processing(db: Session, document_id: UUID, job_id: UUID, *, settings) -> dict:
    _, job = _locked(db, document_id, job_id)
    _enabled(settings, operation=job.operation)
    now = _now(db)
    if not public_job(job, now)["can_retry"] or "pipeline" not in job.checkpoint:
        raise error("DOCUMENT_PROCESSING_RETRY_UNAVAILABLE")
    if db.scalar(select(DocumentProcessingJob.id).where(DocumentProcessingJob.document_id == document_id,
            DocumentProcessingJob.id != job_id, DocumentProcessingJob.status.in_(ACTIVE))):
        raise error("DOCUMENT_PROCESSING_BUSY")
    if job.status == "running":
        job.checkpoint = {**job.checkpoint, "requires_io_reconciliation": True}
    job.checkpoint = {**job.checkpoint, "cancel_requested": False}
    _release(job, "queued", now)
    db.commit()
    return processing_status(db, document_id, job_id)


def cancel_processing(db: Session, document_id: UUID, job_id: UUID) -> dict:
    _, job = _locked(db, document_id, job_id)
    now = _now(db)
    if job.status not in TERMINAL:
        if job.status == "running" and _utc(job.lease_expires_at) > now:
            job.checkpoint = {**job.checkpoint, "cancel_requested": True}
        else:
            if job.status == "running":
                job.checkpoint = {**job.checkpoint, "requires_io_reconciliation": True}
            _release(job, "cancelled", now)
    db.commit()
    return processing_status(db, document_id, job_id)


def reject_managed_step(job, worker_id):
    if "pipeline" in (job.checkpoint or {}) and worker_id is None:
        raise error("DOCUMENT_PROCESSING_MANAGED_TASK")
    if job.checkpoint.get("cancel_requested"):
        raise error("DOCUMENT_PROCESSING_CANCEL_REQUESTED")


def reject_pending_parse(db, document_id):
    # Legacy deployments without M0 can still return their existing schema error.
    if not inspect(db.get_bind()).has_table(DocumentProcessingJob.__tablename__):
        return
    if db.scalar(select(DocumentProcessingJob.id).where(DocumentProcessingJob.document_id == document_id,
            DocumentProcessingJob.status.in_(ACTIVE))):
        raise error("DOCUMENT_PROCESSING_BUSY")


class ProcessingParseLease:
    def __init__(self, document_id, job_id, token, fence):
        self.document_id, self.job_id, self.token, self.fence = document_id, job_id, token, fence

    def check(self, db):
        _, job = _locked(db, self.document_id, self.job_id)
        if (job.status != "running" or (job.lease_token, job.fencing_token) != (self.token, self.fence)
                or _utc(job.lease_expires_at) <= _now(db)):
            raise error("DOCUMENT_PROCESSING_LEASE_LOST")
        return job

    def started(self, db, run_id):
        job = self.check(db)
        job.checkpoint = {**job.checkpoint, "parse_run_id": str(run_id)}

    def writing(self, db):
        begin_external_write(self.check(db))

    def succeeded(self, db, source_version):
        job = self.check(db)
        job.source_version, job.stage = source_version, "source_ready"
        _release(job, "queued", _now(db))


def renew_processing_lease(db, document_id, job_id, worker_id, *, lease_seconds):
    _, job = _locked(db, document_id, job_id)
    now = _now(db)
    if job.status != "running" or job.locked_by != worker_id or _utc(job.lease_expires_at) <= now:
        db.rollback()
        return False
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    db.commit()
    return True


def advance_processing(db: Session, document_id: UUID, job_id: UUID, *, settings, worker_id: str, assets=None,
                       parser=None, graph_provider=None, writer=None, encoder=None, index=None) -> bool:
    """One recoverable stage/batch; never holds a long transaction across IO."""
    document, job = _locked(db, document_id, job_id)
    _enabled(settings, operation=job.operation)
    if job.status != "queued" or "pipeline" not in job.checkpoint:
        db.rollback()
        return False
    if job.checkpoint.get("cancel_requested"):
        _release(job, "cancelled", _now(db))
        db.commit()
        return True
    expected = job.checkpoint["pipeline"]
    source, build, chunk_set, request = job.source_version, job.graph_build_id, job.chunk_set_id, job.request_id
    lease, initial_fence = None, job.fencing_token
    try:
        config = _frozen_pipeline_config(expected)
        if expected != pipeline_contract(settings, config, operation=job.operation):
            raise error("DOCUMENT_PROCESSING_INPUT_CHANGED")
        if source is None:
            if job.attempt_count >= job.max_attempts:
                raise error("DOCUMENT_PROCESSING_ATTEMPTS_EXHAUSTED")
            previous = job.checkpoint.get("parse_run_id")
            if previous:
                run = db.get(DocumentParseRun, UUID(previous))
                if run is None or run.document_id != document_id or run.status == "succeeded":
                    raise error("DOCUMENT_PROCESSING_PARSE_CONFLICT")
                run.status, run.is_active, run.completed_at = "failed", False, _now(db)
                run.error_message = "已由显式任务恢复替代。"
                document.process_status = "parse_failed"
            job.status, job.stage, job.locked_by = "running", "parsing", worker_id
            job.lease_token, job.fencing_token = uuid4(), job.fencing_token + 1
            job.attempt_count += 1
            job.locked_at, job.lease_expires_at = _now(db), _now(db) + timedelta(seconds=settings.kg_lease_seconds)
            job.finished_at = job.last_error_code = None
            lease = ProcessingParseLease(document_id, job_id, job.lease_token, job.fencing_token)
        db.commit()
        if source is None:
            from app.services.document_parsing import parse_document
            (parser or parse_document)(db, document_id, settings=settings, processing_lease=lease)
        elif build is None:
            from app.services.document_graph_builds import prepare_graph_build
            prepare_graph_build(db, document_id, source, request, settings=settings, assets=assets, worker_id=worker_id)
        elif chunk_set is None:
            from app.services.document_graph_builds import advance_graph_build, READY
            from app.models.document_graph_build import GraphBuild
            row = db.get(GraphBuild, build)
            ready = row.status in READY
            db.commit()
            if ready:
                from app.services.document_chunk_sets import prepare_chunk_set
                prepare_chunk_set(db, document_id, source, build, request, config, settings=settings, worker_id=worker_id)
            else:
                advance_graph_build(db, document_id, build, settings=settings, assets=assets,
                    provider=graph_provider, writer=writer, worker_id=worker_id)
        else:
            from app.services.document_chunk_sets import advance_chunk_set
            advance_chunk_set(db, document_id, chunk_set, settings=settings, assets=assets,
                provider=encoder, index=index, worker_id=worker_id)
    except Exception as exc:
        db.rollback()
        # Stage services already record their fenced failures. Pre-stage errors
        # may only terminate an unchanged queued job, never another live owner.
        try:
            _, current = _locked(db, document_id, job_id)
            same_parse = lease and current.status == "running" and (
                current.lease_token, current.fencing_token) == (lease.token, lease.fence)
            unchanged = current.status == "queued" and current.fencing_token == initial_fence and (
                current.source_version, current.graph_build_id, current.chunk_set_id) == (source, build, chunk_set)
            if unchanged or same_parse:
                current.last_error_code = exc.code if isinstance(exc, BusinessError) else "DOCUMENT_PROCESSING_STAGE_FAILED"
                _release(current, "failed", _now(db))
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
        raise
    return True
