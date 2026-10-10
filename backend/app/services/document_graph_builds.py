"""Graph-only stage, with durable per-piece progress and fenced commits.

No chunker, embedding, search publication or online RAG dependency. Every network
operation runs after commit; Document is always the first row lock acquired.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import errors as error_codes
from app.core.config import get_settings
from app.core.errors import BusinessError, LLM_ERROR_STATUS_CODES
from app.extraction.kg_extract import (EXTRACTION_VERSION, TEMPLATE_SHA256, extract_piece,
    extraction_failure_diagnostics, extraction_log_context, merge_parts, strict_json, template)
from app.extraction.kg_protocol import ANCHOR_ADAPTER
from app.extraction.kg_units import RULE_SHA256, RULE_VERSION, make_graph_id, split_units
from app.extraction.kg_writer import configured_writer, validate_payload
from app.ingestion.frozen_source import json_bytes, sha256_bytes, verify_frozen_source
from app.llm.configuration import validate_active_llm_configuration
from app.llm.provider import build_llm_provider
from app.models.document_graph_build import GraphBuild
from app.models.document_processing_job import DocumentProcessingJob
from app.models.document_source_version import SourceDocumentVersion
from app.models.kg_extraction_unit import KGExtractionUnit
from app.services.document_operation_guard import DocumentOperationGuard
from app.services.object_storage import get_object_bytes_from_minio, upload_bytes_to_minio

READY = {"ready", "ready_empty"}
SUCCEEDED = {"succeeded_nonempty", "succeeded_empty"}
logger = logging.getLogger(__name__)
# Trusted application constants, not arbitrary exception strings or detail values.
_DIAGNOSTIC_BUSINESS_CODES = frozenset(
    value for name, value in vars(error_codes).items() if type(value) is str and name == value
) | {"KG_LEASE_LOST", "KG_SOURCE_INVALID", "KG_INDEX_INCONSISTENT"}


def _log_unit_failure(exc: Exception, diagnostics: dict) -> None:
    try:
        code = exc.code if isinstance(exc, BusinessError) and type(exc.code) is str else None
        context = {**extraction_log_context(diagnostics),
            **extraction_failure_diagnostics(exc, diagnostics["phase"]),
            "cause_code": code if code in LLM_ERROR_STATUS_CODES else "UNCLASSIFIED",
            "business_error_code": code if code in _DIAGNOSTIC_BUSINESS_CODES else None,
            "business_error_code_redacted": code is not None and code not in _DIAGNOSTIC_BUSINESS_CODES}
        if diagnostics["phase"] == "provider_close" and exc.__context__ is not None:
            context["prior_exception_type"] = type(exc.__context__).__name__
        logger.warning("KG extraction unit failed: context=%s",
            json.dumps(context, separators=(",", ":")), extra={"event": "kg_extraction_unit_failed", **context})
    except Exception:
        # A diagnostic formatter/handler failure must not alter the stage error or recovery.
        pass


def _error(code: str, status=409) -> BusinessError:
    return BusinessError(code, "知识图谱阶段未完成，请按状态诊断处理后显式重试。", status_code=status)


def _now(db: Session) -> datetime:
    return _utc(db.scalar(select(func.now())))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _snapshot(row) -> dict:
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def require_graph_schema(db: Session) -> None:
    inspector = inspect(db.get_bind())
    for model in (SourceDocumentVersion, GraphBuild, KGExtractionUnit, DocumentProcessingJob):
        name = model.__tablename__
        if not inspector.has_table(name) or not set(model.__table__.columns.keys()).issubset(
                c["name"] for c in inspector.get_columns(name)):
            raise _error("KG_SCHEMA_UNAVAILABLE", 503)


def provider_fingerprint(settings) -> str:
    metadata = validate_active_llm_configuration(settings)
    endpoint = settings.llm_base_url if metadata.provider == "local" else settings.llm_remote_base_url
    return sha256_bytes(json_bytes(dict(provider=metadata.provider, model=metadata.model, endpoint=endpoint,
        extraction_version=EXTRACTION_VERSION, temperature=0.1, max_tokens=settings.kg_llm_max_tokens,
        json_mode=True, timeout=settings.kg_model_timeout_seconds)))


class GraphAssets:
    """Build-owned content-addressed assets; failed attempts are retained."""
    def read(self, bucket: str, key: str, digest: str, *, limit=32_000_000) -> bytes:
        data = get_object_bytes_from_minio(bucket_name=bucket, object_key=key, max_bytes=limit)
        if sha256_bytes(data) != digest:
            raise ValueError("KG asset hash mismatch")
        return data

    def save(self, bucket: str, prefix: str, data: dict) -> tuple[str, str]:
        content = json_bytes(data)
        if len(content) > 32_000_000:
            raise ValueError("KG asset budget exceeded")
        digest = sha256_bytes(content)
        key = f"{prefix}/{digest}.json"
        upload_bytes_to_minio(bucket_name=bucket, object_key=key, content=content, content_type="application/json")
        self.read(bucket, key, digest)
        return key, digest


def _source_text(source: dict, assets: GraphAssets) -> tuple[str, list[dict]]:
    output_prefix = str(PurePosixPath(source["canonical_object_key"]).parent)
    if str(PurePosixPath(source["block_map_object_key"]).parent) != output_prefix:
        raise ValueError("Frozen source asset directories disagree")
    canonical = assets.read(source["bucket_name"], source["canonical_object_key"], source["canonical_sha256"])
    directory = assets.read(source["bucket_name"], source["block_map_object_key"], source["block_map_sha256"])
    text = verify_frozen_source(canonical, directory, output_prefix=output_prefix, **{key: source[key] for key in (
        "source_version", "document_id", "parse_run_id", "canonical_sha256", "block_map_sha256", "character_count")})
    return text, json.loads(directory)["blocks"]


def _source(db: Session, document_id: UUID, source_version: UUID):
    source = db.scalar(select(SourceDocumentVersion).where(
        SourceDocumentVersion.source_version == source_version, SourceDocumentVersion.document_id == document_id))
    if source is None:
        raise _error("KG_SOURCE_NOT_FOUND", 404)
    return source


def _request_job(db, document_id, request_id):
    return db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == document_id,
        DocumentProcessingJob.operation == "process", DocumentProcessingJob.request_id == request_id))


def _existing_request(db, document_id, source_version, request_id):
    job = _request_job(db, document_id, request_id)
    if job is not None:
        if job.source_version == source_version and job.graph_build_id is None and job.checkpoint.get("pipeline"):
            return None
        if job.source_version != source_version or job.graph_build_id is None:
            raise _error("KG_REQUEST_ID_CONFLICT")
        return job.graph_build_id
    return None


def prepare_graph_build(db: Session, document_id: UUID, source_version: UUID, request_id: UUID,
                        *, settings=None, assets=None, worker_id=None) -> dict:
    settings, assets = settings or get_settings(), assets or GraphAssets()
    if not settings.kg_build_enabled:
        raise _error("KG_BUILD_DISABLED", 503)
    require_graph_schema(db)
    document = DocumentOperationGuard(db).lock_normal(document_id)
    from app.services.document_processing import reject_managed_step
    pending = _request_job(db, document_id, request_id)
    if pending:
        reject_managed_step(pending, worker_id)
    existing = _existing_request(db, document_id, source_version, request_id)
    if existing:
        db.commit()
        return graph_build_status(db, document_id, existing)
    if document.file_type.lower() != ".pdf" or document.process_status != "cleaned_source_ready":
        raise _error("KG_SOURCE_NOT_READY")
    source = _snapshot(_source(db, document_id, source_version))
    filename, identity_day = document.original_filename, _now(db).date()
    db.commit()
    fingerprint, pack = provider_fingerprint(settings), template()
    try:
        text, blocks = _source_text(source, assets)
        graph_id = make_graph_id(filename, identity_day)
        drafts = split_units(text, graph_id, block_directory=blocks)
    except Exception as exc:
        raise _error("KG_SOURCE_INVALID", 422) from exc
    if len(drafts) > settings.kg_max_units or any(len(d.pieces) > 128 or
            any(len(p) > settings.kg_max_piece_chars for p in d.pieces) for d in drafts):
        raise _error("KG_EXTRACTION_BUDGET_EXCEEDED", 400)
    input_fingerprint = sha256_bytes(json_bytes(dict(document_id=str(document_id), source_version=str(source_version),
        canonical_sha256=source["canonical_sha256"], block_map_sha256=source["block_map_sha256"],
        filename=filename, provider=fingerprint, template=TEMPLATE_SHA256, unit_rule=RULE_SHA256)))
    build_id, job_id = uuid4(), uuid4()
    try:
        document = DocumentOperationGuard(db).lock_normal(document_id)
        existing = _existing_request(db, document_id, source_version, request_id)
        if existing:
            db.commit()
            return graph_build_status(db, document_id, existing)
        if document.process_status != "cleaned_source_ready":
            raise _error("KG_SOURCE_NOT_READY")
        if db.scalar(select(GraphBuild.id).where(GraphBuild.document_id == document_id, GraphBuild.source_version == source_version)):
            raise _error("KG_BUILD_ALREADY_EXISTS")
        if db.scalar(select(GraphBuild.id).where(GraphBuild.graph_id == graph_id)):
            raise _error("KG_GRAPH_ID_CONFLICT")
        pending = _request_job(db, document_id, request_id)
        if pending:
            reject_managed_step(pending, worker_id)
            if pending.status != "queued" or pending.stage != "source_ready" or pending.source_version != source_version:
                raise _error("KG_DOCUMENT_BUSY")
        active = list(db.scalars(select(DocumentProcessingJob.id).where(DocumentProcessingJob.document_id == document_id,
                DocumentProcessingJob.status.in_(("queued", "running", "retry_wait")))))
        if any(key != (pending.id if pending else None) for key in active):
            raise _error("KG_DOCUMENT_BUSY")
        build = GraphBuild(id=build_id, document_id=document_id, source_version=source_version, graph_id=graph_id,
            identity_day=identity_day, source_path=f"documents/{document_id}/sources/{source_version}/graphs/{build_id}",
            graph_schema_version=2, template_version=pack["version"], template_sha256=TEMPLATE_SHA256,
            unit_rule_version=RULE_VERSION, unit_rule_sha256=RULE_SHA256, provider_fingerprint=fingerprint,
            input_fingerprint=input_fingerprint, status="pending", unit_count=len(drafts), write_checkpoint={})
        db.add(build)
        db.flush()
        for index, draft in enumerate(drafts):
            db.add(KGExtractionUnit(id=uuid4(), document_id=document_id, graph_build_id=build_id,
                source_version=source_version, source_start=draft.source_start, source_end=draft.source_end,
                kind=draft.anchor.anchor_type, unit_index=index, allocated_anchor_id=draft.anchor.anchor_id,
                allocated_anchor_metadata=draft.anchor.business_metadata(), input_sha256=draft.input_sha256,
                status="pending", has_qualified_triples=False, piece_checkpoints={}))
        if pending:
            pending.graph_build_id, pending.stage = build_id, "kg_extracting"
            pending.checkpoint = {**pending.checkpoint, "filename": filename}
            pending.max_attempts += sum(len(d.pieces) for d in drafts) + 4
        else:
            db.add(DocumentProcessingJob(id=job_id, document_id=document_id, operation="process", request_id=request_id,
            input_fingerprint=input_fingerprint, source_version=source_version, graph_build_id=build_id,
            stage="kg_extracting", status="queued", checkpoint={"m2_only": True, "filename": filename},
            # One claim per piece plus final write; three additional explicit
            # failure/lease recovery claims. Counters never reset on retry.
            max_attempts=sum(len(d.pieces) for d in drafts) + 4))
        document.process_status = "kg_extracting"
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error("KG_IDENTITY_CONFLICT") from exc
    except Exception:
        db.rollback()
        raise
    return graph_build_status(db, document_id, build_id)


def _locked(db, document_id, build_id):
    document = DocumentOperationGuard(db).lock_normal(document_id)
    job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == document_id,
        DocumentProcessingJob.graph_build_id == build_id, DocumentProcessingJob.operation == "process")
        .execution_options(populate_existing=True).with_for_update())
    build = db.scalar(select(GraphBuild).where(GraphBuild.id == build_id, GraphBuild.document_id == document_id)
        .execution_options(populate_existing=True).with_for_update())
    if job is None or build is None or job.source_version != build.source_version:
        raise _error("KG_BUILD_NOT_FOUND", 404)
    return document, job, build


def _fenced(db, document_id, build_id, lease):
    document, job, build = _locked(db, document_id, build_id)
    token, fence = lease
    if (job.status != "running" or job.lease_token != token or job.fencing_token != fence
            or _utc(job.lease_expires_at) <= _now(db) or build.status in READY):
        raise _error("KG_LEASE_LOST")
    return document, job, build


def _release(job, status: str, now: datetime):
    from app.services.document_processing import finish_external_write
    finish_external_write(job, status)
    job.status = status
    job.locked_by = job.lease_token = job.locked_at = job.lease_expires_at = None
    job.next_retry_at = None
    job.finished_at = now if status == "failed" else None


def _units(db, build_id):
    return list(db.scalars(select(KGExtractionUnit).where(KGExtractionUnit.graph_build_id == build_id)
                           .order_by(KGExtractionUnit.unit_index).execution_options(populate_existing=True)))


def _verify_drafts(drafts, units):
    if len(drafts) != len(units):
        raise ValueError("KG unit count changed")
    for index, (draft, unit) in enumerate(zip(drafts, units, strict=True)):
        if (unit["unit_index"] != index or unit["input_sha256"] != draft.input_sha256
                or unit["source_start"] != draft.source_start or unit["source_end"] != draft.source_end
                or unit["allocated_anchor_metadata"] != draft.anchor.business_metadata()
                or unit["allocated_anchor_id"] != draft.anchor.anchor_id or unit["kind"] != draft.anchor.anchor_type):
            raise ValueError("Immutable KG unit mismatch")


def _read_result(assets, source, key, digest):
    return strict_json(assets.read(source["bucket_name"], key, digest).decode("utf-8"), max_bytes=32_000_000)


def advance_graph_build(db: Session, document_id: UUID, build_id: UUID, *, retry=False,
                        settings=None, assets=None, provider=None, writer=None, worker_id=None) -> dict:
    settings, assets = settings or get_settings(), assets or GraphAssets()
    if not settings.kg_build_enabled:
        raise _error("KG_BUILD_DISABLED", 503)
    require_graph_schema(db)
    document, job, build = _locked(db, document_id, build_id)
    from app.services.document_processing import reject_managed_step
    reject_managed_step(job, worker_id)
    if build.status in READY:
        db.commit()
        return graph_build_status(db, document_id, build_id)
    now = _now(db)
    if job.status == "running" and _utc(job.lease_expires_at) > now:
        raise _error("KG_BUILD_BUSY")
    if job.status in {"failed", "running"} and not retry:
        raise _error("KG_EXPLICIT_RETRY_REQUIRED")
    if job.status not in {"queued", "running", "failed"} or job.attempt_count >= job.max_attempts:
        raise _error("KG_RETRY_LIMIT_REACHED")
    if (build.template_sha256 != TEMPLATE_SHA256 or build.unit_rule_sha256 != RULE_SHA256
            or build.provider_fingerprint != provider_fingerprint(settings)):
        raise _error("KG_BUILD_INPUT_CHANGED")
    units = _units(db, build_id)
    unit = next((u for u in units if u.status not in SUCCEEDED), None)
    if job.status == "running":
        job.checkpoint = {**job.checkpoint, "requires_io_reconciliation": True}
    job.lease_token, job.fencing_token = uuid4(), job.fencing_token + 1
    lease = (job.lease_token, job.fencing_token)
    job.attempt_count += 1
    job.status, job.locked_by = "running", worker_id or "kg-api-step"
    job.locked_at, job.lease_expires_at = now, now + timedelta(seconds=settings.kg_lease_seconds)
    job.finished_at = job.next_retry_at = job.last_error_code = None
    job.stage = "kg_extracting" if unit else "kg_writing"
    build.status, build.last_error_code = ("extracting" if unit else "writing"), None
    document.process_status = "kg_extracting" if unit else "kg_writing"
    if unit:
        unit.status, unit.last_error_code = "extracting", None
    source = _snapshot(_source(db, document_id, build.source_version))
    build_data, all_units, filename = _snapshot(build), [_snapshot(u) for u in units], job.checkpoint["filename"]
    unit_data = _snapshot(unit) if unit else None
    diagnostics = dict(document_id=str(document_id), graph_build_id=str(build_id), job_id=str(job.id),
        unit_id=str(unit_data["id"]) if unit_data else None,
        unit_index=unit_data["unit_index"] if unit_data else None,
        unit_kind=unit_data["kind"] if unit_data else None,
        piece_index=None, phase="source_read", effective_max_tokens=settings.kg_llm_max_tokens)
    db.commit()
    try:
        text, blocks = _source_text(source, assets)
        diagnostics["phase"] = "unit_split"
        drafts = split_units(text, build_data["graph_id"], block_directory=blocks)
        diagnostics["phase"] = "unit_verify"
        _verify_drafts(drafts, all_units)
        if unit_data:
            _advance_unit(db, document_id, build_id, lease, unit_data, drafts[unit_data["unit_index"]],
                          source, build_data, filename, settings, assets, provider, diagnostics)
        else:
            _finish_build(db, document_id, build_id, lease, all_units, source, build_data, settings, assets, writer)
    except Exception as exc:
        db.rollback()
        if unit_data:
            _log_unit_failure(exc, diagnostics)
        try:
            document, job, build = _fenced(db, document_id, build_id, lease)
            code = "KG_EXTRACTION_FAILED" if unit_data else "KG_WRITE_FAILED"
            if unit_data:
                unit = db.get(KGExtractionUnit, unit_data["id"])
                unit.status = "partial_failed" if unit.piece_checkpoints else "failed"
                unit.last_error_code = code
                partial = bool(unit.piece_checkpoints) or any(u["status"] in SUCCEEDED for u in all_units)
                build.status = "partial_failed" if partial else "extraction_failed"
            else:
                build.status = "write_failed"
            build.last_error_code = job.last_error_code = code
            document.process_status = "kg_failed"
            _release(job, "failed", _now(db))
            db.commit()
        except Exception:
            db.rollback()
            # A stale executor must never overwrite the current owner's state.
            raise _error("KG_LEASE_LOST") from exc
        raise _error(code, 502) from exc
    return graph_build_status(db, document_id, build_id)


def _advance_unit(db, document_id, build_id, lease, unit, draft, source, build, filename, settings, assets, provider, diagnostics):
    diagnostics["phase"] = "piece_checkpoint_validation"
    checkpoints = dict(unit["piece_checkpoints"])
    if any(key not in {str(i) for i in range(len(draft.pieces))} for key in checkpoints):
        raise ValueError("Invalid piece checkpoint index")
    saved_parts = {}
    for piece_index, checkpoint in checkpoints.items():
        diagnostics.update(phase="piece_checkpoint_read", piece_index=int(piece_index))
        saved = _read_result(assets, source, checkpoint["object_key"], checkpoint["sha256"])
        if (saved["unit_id"] != str(unit["id"]) or saved["input_sha256"] != unit["input_sha256"]
                or saved["piece_index"] != int(piece_index)):
            raise ValueError("Piece checkpoint ownership mismatch")
        saved_parts[piece_index] = saved["part"]
    diagnostics.update(phase="before_llm_fence", piece_index=None)
    _fenced(db, document_id, build_id, lease)
    diagnostics["phase"] = "before_llm_commit"
    db.commit()
    diagnostics["phase"] = "piece_selection"
    index = next(i for i in range(len(draft.pieces)) if str(i) not in checkpoints)
    diagnostics.update(phase="provider_init", piece_index=index)
    owns_provider = provider is None
    if owns_provider:
        provider = build_llm_provider(settings)
    try:
        part = extract_piece(provider, text=draft.pieces[index], filename=filename,
            anchor_metadata=unit["allocated_anchor_metadata"], piece_index=index,
            timeout_seconds=settings.kg_model_timeout_seconds, max_tokens=settings.kg_llm_max_tokens,
            diagnostics=diagnostics)
    finally:
        if owns_provider:
            try:
                provider.close()
            except Exception:
                diagnostics["prior_phase"] = diagnostics["phase"]
                diagnostics["phase"] = "provider_close"
                raise
    # The model may finish after lease takeover or deletion admission. Recheck
    # before writing even an orphaned object, then release SQL before storage IO.
    diagnostics["phase"] = "before_asset_fence"
    _, job, _ = _fenced(db, document_id, build_id, lease)
    from app.services.document_processing import begin_external_write
    begin_external_write(job)
    diagnostics["phase"] = "external_write_commit"
    db.commit()
    prefix = f"kg-assets/{document_id}/{source['source_version']}/{build_id}/units/{unit['id']}"
    envelope = dict(unit_id=str(unit["id"]), input_sha256=unit["input_sha256"], piece_index=index, part=part)
    diagnostics["phase"] = "piece_asset_save"
    key, digest = assets.save(source["bucket_name"], f"{prefix}/pieces", envelope)
    checkpoints[str(index)] = dict(object_key=key, sha256=digest)
    saved_parts[str(index)] = part
    completed = len(checkpoints) == len(draft.pieces)
    result = result_key = result_hash = None
    if completed:
        diagnostics["phase"] = "merge_parts"
        parts = [saved_parts[str(i)] for i in range(len(draft.pieces))]
        result = merge_parts(parts)
        if not result["relationships"]:
            result["entities"] = []
        diagnostics["phase"] = "unit_result_save"
        result_key, result_hash = assets.save(source["bucket_name"], f"{prefix}/result", dict(
            unit_id=str(unit["id"]), input_sha256=unit["input_sha256"], result=result))
    diagnostics["phase"] = "after_asset_fence"
    _, job, _ = _fenced(db, document_id, build_id, lease)
    diagnostics["phase"] = "unit_checkpoint_validation"
    row = db.get(KGExtractionUnit, unit["id"])
    if row.status != "extracting" or row.piece_checkpoints != unit["piece_checkpoints"]:
        raise ValueError("Concurrent unit change")
    diagnostics["phase"] = "unit_state_update"
    row.piece_checkpoints = checkpoints
    if completed:
        row.has_qualified_triples = bool(result["relationships"])
        row.status = "succeeded_nonempty" if row.has_qualified_triples else "succeeded_empty"
        row.result_object_key, row.result_sha256, row.completed_at = result_key, result_hash, _now(db)
    else:
        row.status = "pending"
    _release(job, "queued", _now(db))
    diagnostics["phase"] = "unit_commit"
    db.commit()


def _finish_build(db, document_id, build_id, lease, units, source, build, settings, assets, writer):
    results = []
    for unit in units:
        if unit["status"] not in SUCCEEDED:
            raise ValueError("Cannot publish incomplete extraction")
        envelope = _read_result(assets, source, unit["result_object_key"], unit["result_sha256"])
        if envelope["unit_id"] != str(unit["id"]) or envelope["input_sha256"] != unit["input_sha256"]:
            raise ValueError("Unit result ownership mismatch")
        if bool(envelope["result"]["relationships"]) != unit["has_qualified_triples"]:
            raise ValueError("Unit qualification mismatch")
        results.append(envelope["result"])
    payload = dict(graph_schema_version=2, document_id=str(document_id), source_version=str(source["source_version"]),
        graph_build_id=str(build_id), graph_id=build["graph_id"], source_path=build["source_path"],
        canonical_sha256=source["canonical_sha256"], character_count=source["character_count"],
        template_version=build["template_version"], units=[dict(unit_id=str(u["id"]),
            source_start=u["source_start"], source_end=u["source_end"], anchor=u["allocated_anchor_metadata"],
            eligible=u["has_qualified_triples"]) for u in units], **merge_parts(results))
    validate_payload(payload)
    _, job, _ = _fenced(db, document_id, build_id, lease)
    from app.services.document_processing import begin_external_write
    begin_external_write(job)
    db.commit()
    key, digest = assets.save(source["bucket_name"], f"kg-assets/{document_id}/{source['source_version']}/{build_id}/result", payload)
    _, _, row = _fenced(db, document_id, build_id, lease)
    previous = row.write_checkpoint
    if previous and (previous.get("payload_sha256") != digest or previous.get("object_key") != key):
        raise ValueError("Immutable write manifest changed")
    row.write_checkpoint = dict(payload_sha256=digest, object_key=key)
    db.commit()
    receipt = None
    if payload["relationships"]:
        owns_writer = writer is None
        if owns_writer:
            writer = configured_writer(settings)
        try:
            receipt = writer.write(payload)
        finally:
            if owns_writer:
                writer.driver.close()
        if receipt != dict(payload_sha256=digest, entities=len(payload["entities"]),
                relationships=len(payload["relationships"]), graph_build_id=str(build_id), status="built"):
            raise ValueError("Graph receipt mismatch")
    document, job, row = _fenced(db, document_id, build_id, lease)
    # Revalidate counts/statuses in the same transaction that seals the parent.
    current = _units(db, build_id)
    if [_snapshot(u) for u in current] != units:
        raise ValueError("Units changed before publication")
    row.unit_count, row.anchor_count = len(units), sum(u["has_qualified_triples"] for u in units)
    row.entity_count, row.relationship_count = len(payload["entities"]), len(payload["relationships"])
    row.result_object_key, row.result_sha256, row.sealed_at = key, digest, _now(db)
    row.status = "ready" if row.anchor_count else "ready_empty"
    row.write_checkpoint = dict(payload_sha256=digest, object_key=key, receipt=receipt)
    document.process_status = "kg_ready" if row.anchor_count else "kg_ready_empty"
    job.stage, job.checkpoint = "kg_ready", {**job.checkpoint, "awaiting_stage": "chunking"}
    _release(job, "queued", _now(db))
    db.commit()


def graph_build_status(db: Session, document_id: UUID, build_id: UUID) -> dict:
    require_graph_schema(db)
    DocumentOperationGuard(db).lock_normal(document_id)
    build = db.scalar(select(GraphBuild).where(GraphBuild.id == build_id, GraphBuild.document_id == document_id))
    job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.graph_build_id == build_id,
        DocumentProcessingJob.document_id == document_id, DocumentProcessingJob.operation == "process"))
    if build is None or job is None:
        raise _error("KG_BUILD_NOT_FOUND", 404)
    counts = {}
    for unit in _units(db, build_id):
        counts[unit.status] = counts.get(unit.status, 0) + 1
    result = dict(graph_build_id=build.id, document_id=document_id, source_version=build.source_version,
        graph_id=build.graph_id, status=build.status, job_status=job.status, stage=job.stage,
        unit_counts=counts, anchor_count=build.anchor_count, entity_count=build.entity_count,
        relationship_count=build.relationship_count, error_code=build.last_error_code,
        attempt_count=job.attempt_count, max_attempts=job.max_attempts,
        lease_expires_at=job.lease_expires_at, can_advance=build.status not in READY,
        next_stage="chunking" if build.status in READY else None)
    db.commit()
    return result


def load_anchor_index(db: Session, document_id: UUID, source_version: UUID, build_id: UUID) -> tuple[dict, ...]:
    """Authoritative index for later stages, independent of any chunks."""
    DocumentOperationGuard(db).lock_normal(document_id)
    build = db.scalar(select(GraphBuild).where(GraphBuild.id == build_id, GraphBuild.document_id == document_id,
        GraphBuild.source_version == source_version, GraphBuild.status.in_(READY), GraphBuild.sealed_at.is_not(None)))
    if build is None:
        raise _error("KG_BUILD_NOT_READY")
    source = _source(db, document_id, source_version)
    result = []
    for unit in _units(db, build_id):
        if unit.status not in SUCCEEDED:
            raise _error("KG_INDEX_INCONSISTENT")
        if unit.has_qualified_triples:
            anchor = ANCHOR_ADAPTER.validate_python(unit.allocated_anchor_metadata)
            if (anchor.graph_id != build.graph_id or anchor.anchor_id != unit.allocated_anchor_id
                    or not 0 <= unit.source_start < unit.source_end <= source.character_count):
                raise _error("KG_INDEX_INCONSISTENT")
            result.append(dict(document_id=document_id, source_version=source_version, graph_build_id=build_id,
                unit_id=unit.id, source_start=unit.source_start, source_end=unit.source_end,
                anchor_metadata=anchor.business_metadata(), publication_status=build.status))
    if len(result) != build.anchor_count:
        raise _error("KG_INDEX_INCONSISTENT")
    db.commit()
    return tuple(result)
