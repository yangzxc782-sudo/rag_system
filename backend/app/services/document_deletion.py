from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import (
    DOCUMENT_DELETION_EXECUTOR_DISABLED,
    DOCUMENT_DELETION_NOT_FAILED,
    DOCUMENT_DELETION_RETRY_REQUIRED,
    DOCUMENT_DELETION_STATE_INCONSISTENT,
    BusinessError,
)
from app.models.document import Document
from app.models.document_asset import DocumentAsset
from app.models.document_block import DocumentBlock
from app.models.document_chunk import DocumentChunk
from app.models.document_chunk_block import DocumentChunkBlock
from app.models.document_deletion_job import DocumentDeletionJob
from app.models.document_parse_run import DocumentParseRun
from app.models.knowledge_item_chunk import KnowledgeItemChunk
from app.search_engine.client import SearchEngineClientProtocol
from app.services.document_deletion_manifest import (
    DocumentDeletionManifest,
    DocumentDeletionManifestError,
    build_document_deletion_manifest,
)
from app.services.document_deletion_minio import (
    delete_minio_derived_targets,
    delete_minio_raw_target,
)
from app.services.document_deletion_opensearch import delete_opensearch_targets
from app.services.document_deletion_repository import (
    build_advance_step_statement,
    claim_next_job,
    record_step_failure,
    renew_lease,
    manual_retry_job,
)
from app.services.document_deletion_state import validate_document_job_invariant
from app.services.document_deletion_storage import (
    DOCUMENT_DELETION_LEASE_LOST,
    DocumentDeletionLeaseLost,
    DocumentDeletionStorageError,
    StorageCheckpoint,
)
from app.services.document_knowledge_deletion import (
    DocumentKnowledgeDeletionInvariantError,
    DocumentKnowledgeDeletionService,
)


DOCUMENT_DELETION_STEP_FAILED = "DOCUMENT_DELETION_STEP_FAILED"
DOCUMENT_KNOWLEDGE_DELETION_INVARIANT = (
    "DOCUMENT_KNOWLEDGE_DELETION_INVARIANT"
)
DOCUMENT_DELETION_RETRIES_EXHAUSTED = "DOCUMENT_DELETION_RETRIES_EXHAUSTED"

_NEXT_STEP = {
    "delete_opensearch": "delete_minio_derived",
    "delete_minio_derived": "delete_minio_raw",
    "delete_minio_raw": "finalize_postgresql",
}
_RETRY_MULTIPLIER = 3


@dataclass(frozen=True, slots=True)
class ClaimedDocumentDeletion:
    job_id: UUID
    document_id: UUID
    current_step: str
    step_attempts: int
    max_attempts: int
    manifest: dict[str, Any]
    lease_token: UUID

    @classmethod
    def from_job(cls, job: DocumentDeletionJob) -> "ClaimedDocumentDeletion":
        if job.lease_token is None:
            raise DocumentDeletionLeaseLost()
        return cls(
            job_id=job.id,
            document_id=job.document_id,
            current_step=job.current_step,
            step_attempts=job.step_attempts,
            max_attempts=job.max_attempts,
            manifest=deepcopy(job.manifest),
            lease_token=job.lease_token,
        )


@dataclass(frozen=True, slots=True)
class DocumentDeletionStatus:
    document_id: UUID
    status: str
    step_attempts: int
    next_retry_at: datetime | None
    last_error_code: str | None
    updated_at: datetime


def request_document_deletion(
    db: Session,
    document_id: UUID,
    *,
    settings: Any,
) -> DocumentDeletionStatus | None:
    """Atomically schedule or return one idempotent deletion job."""

    try:
        document, job = _load_locked_pair_by_document_id(db, document_id)
        validate_document_job_invariant(document, job)
        _require_executor_enabled(settings)
        if document is None:
            if job is None:
                db.commit()
                return None
            if job.status == "delete_failed":
                _raise_retry_required(document_id)
            result = _public_status(document_id, document, job)
            db.commit()
            return result

        if document.deletion_status == "delete_failed":
            _raise_retry_required(document_id)
        if job is not None:
            result = _public_status(document_id, document, job)
            db.commit()
            return result

        manifest = build_document_deletion_manifest(
            db,
            document=document,
            settings=settings,
        )
        document.deletion_status = "deleting"
        document.updated_at = func.now()
        job = DocumentDeletionJob(
            document_id=document_id,
            status="pending",
            current_step="delete_opensearch",
            step_attempts=0,
            max_attempts=int(settings.document_deletion_max_step_attempts),
            manifest=manifest.to_payload(),
        )
        db.add(job)
        db.flush()
        result = _public_status(document_id, document, job)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def get_document_deletion_status(
    db: Session,
    document_id: UUID,
) -> DocumentDeletionStatus | None:
    document = db.get(Document, document_id)
    job = db.scalar(
        select(DocumentDeletionJob).where(
            DocumentDeletionJob.document_id == document_id
        )
    )
    try:
        validate_document_job_invariant(document, job)
    except BusinessError as exc:
        raise BusinessError(
            exc.code,
            exc.message,
            detail=exc.detail,
            status_code=500,
        ) from exc
    if document is None and job is None:
        return None
    return _public_status(document_id, document, job)


def retry_document_deletion(
    db: Session,
    document_id: UUID,
    *,
    settings: Any,
) -> DocumentDeletionStatus | None:
    try:
        document, job = _load_locked_pair_by_document_id(db, document_id)
        validate_document_job_invariant(document, job)
        _require_executor_enabled(settings)
        if document is None and job is None:
            db.commit()
            return None
        if job is None:
            raise BusinessError(
                DOCUMENT_DELETION_NOT_FAILED,
                "Document deletion has not failed.",
                detail={"document_id": str(document_id)},
                status_code=409,
            )
        if job.status in {"pending", "processing", "retry_wait"}:
            result = _public_status(document_id, document, job)
            db.commit()
            return result
        if not manual_retry_job(db, job.id):
            raise BusinessError(
                DOCUMENT_DELETION_STATE_INCONSISTENT,
                "Document deletion state is inconsistent.",
                detail={"document_id": str(document_id)},
                status_code=500,
            )
        db.flush()
        db.refresh(job)
        if document is not None:
            db.refresh(document)
        result = _public_status(document_id, document, job)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _load_locked_pair_by_document_id(
    db: Session,
    document_id: UUID,
) -> tuple[Document | None, DocumentDeletionJob | None]:
    document = db.scalar(
        select(Document)
        .where(Document.id == document_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    job = db.scalar(
        select(DocumentDeletionJob)
        .where(DocumentDeletionJob.document_id == document_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return document, job


def _public_status(
    document_id: UUID,
    document: Document | None,
    job: DocumentDeletionJob | None,
) -> DocumentDeletionStatus:
    if job is None:
        if document is None:
            raise AssertionError("completed deletion has no public status")
        return DocumentDeletionStatus(
            document_id=document_id,
            status="normal",
            step_attempts=0,
            next_retry_at=None,
            last_error_code=None,
            updated_at=document.updated_at,
        )
    public_status = {
        "pending": "deleting",
        "processing": "deleting",
        "retry_wait": "retrying",
        "delete_failed": "delete_failed",
    }[job.status]
    return DocumentDeletionStatus(
        document_id=document_id,
        status=public_status,
        step_attempts=job.step_attempts,
        next_retry_at=job.next_retry_at,
        last_error_code=job.last_error_code,
        updated_at=job.updated_at,
    )


def _raise_retry_required(document_id: UUID) -> None:
    raise BusinessError(
        DOCUMENT_DELETION_RETRY_REQUIRED,
        "Document deletion failed; use the retry endpoint.",
        detail={"document_id": str(document_id)},
        status_code=409,
    )


def _require_executor_enabled(settings: Any) -> None:
    if bool(settings.document_deletion_executor_enabled):
        return
    raise BusinessError(
        DOCUMENT_DELETION_EXECUTOR_DISABLED,
        "Document deletion executor is disabled.",
        status_code=503,
    )


class DeletionSagaProtocol(Protocol):
    def run_step(
        self,
        claimed: ClaimedDocumentDeletion,
        *,
        checkpoint: StorageCheckpoint,
    ) -> None: ...


def retry_delay_seconds(
    step_attempts: int,
    *,
    base_seconds: int,
    max_seconds: int,
) -> int:
    if step_attempts <= 0 or base_seconds <= 0 or max_seconds <= 0:
        raise ValueError("retry timing values must be positive")
    return min(
        max_seconds,
        base_seconds * (_RETRY_MULTIPLIER ** (step_attempts - 1)),
    )


def safe_deletion_error_code(exc: Exception) -> str:
    if isinstance(exc, (DocumentDeletionStorageError, DocumentDeletionManifestError)):
        return exc.code
    if isinstance(exc, BusinessError):
        return exc.code
    if isinstance(exc, DocumentKnowledgeDeletionInvariantError):
        return DOCUMENT_KNOWLEDGE_DELETION_INVARIANT
    return DOCUMENT_DELETION_STEP_FAILED


class DocumentDeletionSaga:
    """Execute one already-claimed durable Saga step."""

    def __init__(
        self,
        *,
        settings: Any,
        minio_client: Any,
        opensearch_client: SearchEngineClientProtocol,
        advance_step: Callable[[ClaimedDocumentDeletion, str], None],
        finalize_step: Callable[
            [ClaimedDocumentDeletion, DocumentDeletionManifest], None
        ],
        opensearch_delete: Callable[..., None] = delete_opensearch_targets,
        minio_derived_delete: Callable[..., None] = delete_minio_derived_targets,
        minio_raw_delete: Callable[..., None] = delete_minio_raw_target,
    ) -> None:
        self._settings = settings
        self._minio_client = minio_client
        self._opensearch_client = opensearch_client
        self._advance_step = advance_step
        self._finalize_step = finalize_step
        self._opensearch_delete = opensearch_delete
        self._minio_derived_delete = minio_derived_delete
        self._minio_raw_delete = minio_raw_delete

    def run_step(
        self,
        claimed: ClaimedDocumentDeletion,
        *,
        checkpoint: StorageCheckpoint,
    ) -> None:
        manifest = DocumentDeletionManifest.from_payload(claimed.manifest)
        if manifest.document_id != claimed.document_id:
            raise DocumentDeletionManifestError(
                "DOCUMENT_DELETION_MANIFEST_INVALID",
                "Document deletion manifest identity does not match the job.",
            )

        checkpoint()
        if claimed.current_step == "delete_opensearch":
            self._opensearch_delete(
                manifest,
                client=self._opensearch_client,
                checkpoint=checkpoint,
                current_index_name=self._settings.search_index_name,
                current_index_alias=self._settings.search_index_alias,
                timeout_seconds=int(
                    self._settings.document_deletion_storage_timeout_seconds
                ),
            )
            checkpoint()
            self._advance_step(claimed, _NEXT_STEP[claimed.current_step])
            return

        heartbeat_interval = float(
            self._settings.document_deletion_lease_seconds
        ) / 3.0
        if claimed.current_step == "delete_minio_derived":
            self._minio_derived_delete(
                manifest,
                client=self._minio_client,
                checkpoint=checkpoint,
                heartbeat_interval_seconds=heartbeat_interval,
                heartbeat_object_interval=int(
                    self._settings.document_deletion_heartbeat_object_interval
                ),
            )
            checkpoint()
            self._advance_step(claimed, _NEXT_STEP[claimed.current_step])
            return

        if claimed.current_step == "delete_minio_raw":
            self._minio_raw_delete(
                manifest,
                client=self._minio_client,
                checkpoint=checkpoint,
                heartbeat_interval_seconds=heartbeat_interval,
                heartbeat_object_interval=int(
                    self._settings.document_deletion_heartbeat_object_interval
                ),
            )
            checkpoint()
            self._advance_step(claimed, _NEXT_STEP[claimed.current_step])
            return

        if claimed.current_step == "finalize_postgresql":
            self._finalize_step(claimed, manifest)
            return

        raise DocumentDeletionManifestError(
            "DOCUMENT_DELETION_MANIFEST_INVALID",
            "Document deletion job step is unsupported.",
        )


def claim_document_deletion(
    db: Session,
    *,
    locked_by: str,
    lease_token: UUID,
    lease_seconds: int,
) -> ClaimedDocumentDeletion | None:
    job = claim_next_job(
        db,
        locked_by=locked_by,
        lease_token=lease_token,
        lease_seconds=lease_seconds,
    )
    if job is None:
        return None
    document = db.scalar(
        select(Document).where(Document.id == job.document_id).with_for_update()
    )
    validate_document_job_invariant(document, job)
    return ClaimedDocumentDeletion.from_job(job)


def renew_claimed_deletion(
    db: Session,
    claimed: ClaimedDocumentDeletion,
    *,
    lease_seconds: int,
) -> bool:
    job, _document = _load_locked_pair(db, claimed.job_id)
    if job is None or not _same_processing_owner(job, claimed):
        return False
    return renew_lease(
        db,
        claimed.job_id,
        claimed.lease_token,
        lease_seconds=lease_seconds,
    )


def advance_claimed_deletion(
    db: Session,
    claimed: ClaimedDocumentDeletion,
    *,
    next_step: str,
) -> bool:
    job, _document = _load_locked_pair(db, claimed.job_id)
    if job is None or not _same_processing_owner(job, claimed):
        return False
    result = db.execute(
        build_advance_step_statement(
            claimed.job_id,
            claimed.lease_token,
            next_step,
        )
    )
    return result.rowcount == 1


def fail_claimed_deletion(
    db: Session,
    claimed: ClaimedDocumentDeletion,
    *,
    retry_seconds: int,
    error_code: str,
) -> bool:
    job, _document = _load_locked_pair(db, claimed.job_id)
    if job is None or not _same_processing_owner(job, claimed):
        return False
    return record_step_failure(
        db,
        claimed.job_id,
        claimed.lease_token,
        retry_seconds=retry_seconds,
        error_code=error_code,
    )


def build_exhausted_recovery_select_statement():
    return (
        select(DocumentDeletionJob)
        .where(
            DocumentDeletionJob.status == "processing",
            DocumentDeletionJob.lease_expires_at <= func.now(),
            DocumentDeletionJob.step_attempts
            >= DocumentDeletionJob.max_attempts,
        )
        .order_by(
            DocumentDeletionJob.created_at,
            DocumentDeletionJob.id,
        )
        .with_for_update(skip_locked=True)
    )


def sweep_exhausted_deletions(db: Session) -> tuple[UUID, ...]:
    jobs = tuple(db.scalars(build_exhausted_recovery_select_statement()).all())
    swept_ids: list[UUID] = []
    for job in jobs:
        document = db.scalar(
            select(Document)
            .where(Document.id == job.document_id)
            .with_for_update()
        )
        validate_document_job_invariant(document, job)
        job.status = "delete_failed"
        job.locked_by = None
        job.lease_token = None
        job.locked_at = None
        job.lease_expires_at = None
        job.next_retry_at = None
        job.last_error_code = DOCUMENT_DELETION_RETRIES_EXHAUSTED
        job.updated_at = func.now()
        if document is not None:
            document.deletion_status = "delete_failed"
            document.updated_at = func.now()
        swept_ids.append(job.id)
    if swept_ids:
        db.flush()
    return tuple(swept_ids)


def finalize_postgresql_deletion(
    db: Session,
    *,
    claimed: ClaimedDocumentDeletion,
    manifest: DocumentDeletionManifest,
    knowledge_cleanup: Callable[[UUID], object] | None = None,
) -> None:
    job = db.scalar(
        select(DocumentDeletionJob)
        .where(
            DocumentDeletionJob.id == claimed.job_id,
            DocumentDeletionJob.status == "processing",
            DocumentDeletionJob.current_step == "finalize_postgresql",
            DocumentDeletionJob.lease_token == claimed.lease_token,
            DocumentDeletionJob.lease_expires_at > func.now(),
        )
        .with_for_update()
    )
    if job is None:
        raise DocumentDeletionLeaseLost()
    document = db.scalar(
        select(Document)
        .where(Document.id == claimed.document_id)
        .with_for_update()
    )
    validate_document_job_invariant(document, job)
    if (
        job.document_id != claimed.document_id
        or manifest.document_id != claimed.document_id
    ):
        raise DocumentDeletionLeaseLost()

    cleanup = knowledge_cleanup
    if cleanup is None:
        cleanup = DocumentKnowledgeDeletionService(db).cleanup
    cleanup(claimed.document_id)

    db.execute(
        delete(DocumentChunkBlock).where(
            or_(
                DocumentChunkBlock.chunk_id.in_(manifest.chunk_ids),
                DocumentChunkBlock.block_id.in_(manifest.block_ids),
            )
        )
    )
    db.execute(
        delete(KnowledgeItemChunk).where(
            or_(
                KnowledgeItemChunk.document_id == claimed.document_id,
                KnowledgeItemChunk.chunk_id.in_(manifest.chunk_ids),
            )
        )
    )
    db.execute(
        delete(DocumentChunk).where(
            or_(
                DocumentChunk.document_id == claimed.document_id,
                DocumentChunk.id.in_(manifest.chunk_ids),
            )
        )
    )
    db.execute(
        delete(DocumentAsset).where(
            or_(
                DocumentAsset.document_id == claimed.document_id,
                DocumentAsset.id.in_(manifest.asset_ids),
            )
        )
    )
    db.execute(
        delete(DocumentBlock).where(
            or_(
                DocumentBlock.document_id == claimed.document_id,
                DocumentBlock.id.in_(manifest.block_ids),
            )
        )
    )
    db.execute(
        delete(DocumentParseRun).where(
            or_(
                DocumentParseRun.document_id == claimed.document_id,
                DocumentParseRun.id.in_(manifest.parse_run_ids),
            )
        )
    )
    db.execute(delete(Document).where(Document.id == claimed.document_id))
    db.flush()
    job_delete = db.execute(
        delete(DocumentDeletionJob).where(
            DocumentDeletionJob.id == claimed.job_id,
            DocumentDeletionJob.status == "processing",
            DocumentDeletionJob.current_step == "finalize_postgresql",
            DocumentDeletionJob.lease_token == claimed.lease_token,
        )
    )
    if job_delete.rowcount != 1:
        raise DocumentDeletionLeaseLost()
    db.flush()


def _load_locked_pair(
    db: Session,
    job_id: UUID,
) -> tuple[DocumentDeletionJob | None, Document | None]:
    job = db.scalar(
        select(DocumentDeletionJob)
        .where(DocumentDeletionJob.id == job_id)
        .with_for_update()
    )
    if job is None:
        return None, None
    document = db.scalar(
        select(Document).where(Document.id == job.document_id).with_for_update()
    )
    validate_document_job_invariant(document, job)
    return job, document


def _same_processing_owner(
    job: DocumentDeletionJob,
    claimed: ClaimedDocumentDeletion,
) -> bool:
    return (
        job.status == "processing"
        and job.document_id == claimed.document_id
        and job.current_step == claimed.current_step
        and job.lease_token == claimed.lease_token
    )
