from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
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
