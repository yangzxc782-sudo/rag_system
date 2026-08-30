from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from app.models.document import Document
from app.models.document_deletion_job import DocumentDeletionJob


def _db_interval(seconds: int) -> object:
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    return func.make_interval(0, 0, 0, 0, 0, 0, seconds)


def build_claim_statement(
    *,
    locked_by: str,
    lease_token: UUID,
    lease_seconds: int,
) -> Update:
    now = func.now()
    below_step_limit = DocumentDeletionJob.step_attempts < DocumentDeletionJob.max_attempts
    due = or_(
        DocumentDeletionJob.status == "pending",
        and_(
            DocumentDeletionJob.status == "retry_wait",
            DocumentDeletionJob.next_retry_at <= now,
        ),
        and_(
            DocumentDeletionJob.status == "processing",
            DocumentDeletionJob.lease_expires_at <= now,
        ),
    )
    candidate = (
        select(DocumentDeletionJob.id)
        .where(below_step_limit, due)
        .order_by(DocumentDeletionJob.created_at.asc(), DocumentDeletionJob.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
        .cte("claimable_job")
    )
    return (
        update(DocumentDeletionJob)
        .where(DocumentDeletionJob.id == select(candidate.c.id).scalar_subquery())
        .values(
            status="processing",
            step_attempts=DocumentDeletionJob.step_attempts + 1,
            locked_by=locked_by,
            lease_token=lease_token,
            locked_at=now,
            lease_expires_at=now + _db_interval(lease_seconds),
            next_retry_at=None,
            updated_at=now,
        )
        .returning(DocumentDeletionJob)
    )


def claim_next_job(
    db: Session,
    *,
    locked_by: str,
    lease_token: UUID,
    lease_seconds: int,
) -> DocumentDeletionJob | None:
    result = db.execute(
        build_claim_statement(
            locked_by=locked_by,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
        )
    )
    return result.scalar_one_or_none()


def build_exhausted_sweep_statement(
    *,
    error_code: str = "DOCUMENT_DELETION_RETRIES_EXHAUSTED",
) -> Update:
    now = func.now()
    return (
        update(DocumentDeletionJob)
        .where(
            DocumentDeletionJob.status == "processing",
            DocumentDeletionJob.lease_expires_at <= now,
            DocumentDeletionJob.step_attempts >= DocumentDeletionJob.max_attempts,
        )
        .values(
            status="delete_failed",
            locked_by=None,
            lease_token=None,
            locked_at=None,
            lease_expires_at=None,
            next_retry_at=None,
            last_error_code=error_code,
            updated_at=now,
        )
        .returning(DocumentDeletionJob.id)
    )


def sweep_exhausted_jobs(db: Session) -> list[UUID]:
    job_ids = list(db.scalars(build_exhausted_sweep_statement()).all())
    if job_ids:
        db.execute(_build_exhausted_document_sync_statement())
    return job_ids


def build_renew_lease_statement(
    job_id: UUID,
    lease_token: UUID,
    *,
    lease_seconds: int,
) -> Update:
    now = func.now()
    return (
        update(DocumentDeletionJob)
        .where(
            DocumentDeletionJob.id == job_id,
            DocumentDeletionJob.status == "processing",
            DocumentDeletionJob.lease_token == lease_token,
            DocumentDeletionJob.lease_expires_at > now,
        )
        .values(
            lease_expires_at=now + _db_interval(lease_seconds),
            updated_at=now,
        )
    )


def renew_lease(
    db: Session,
    job_id: UUID,
    lease_token: UUID,
    *,
    lease_seconds: int,
) -> bool:
    result = db.execute(
        build_renew_lease_statement(job_id, lease_token, lease_seconds=lease_seconds)
    )
    return result.rowcount == 1


def build_record_step_failure_statement(
    job_id: UUID,
    lease_token: UUID,
    *,
    retry_seconds: int,
    error_code: str,
) -> Update:
    now = func.now()
    exhausted = DocumentDeletionJob.step_attempts >= DocumentDeletionJob.max_attempts
    return (
        update(DocumentDeletionJob)
        .where(
            DocumentDeletionJob.id == job_id,
            DocumentDeletionJob.status == "processing",
            DocumentDeletionJob.lease_token == lease_token,
            DocumentDeletionJob.lease_expires_at > now,
        )
        .values(
            status=case((exhausted, "delete_failed"), else_="retry_wait"),
            locked_by=None,
            lease_token=None,
            locked_at=None,
            lease_expires_at=None,
            next_retry_at=case(
                (exhausted, None),
                else_=now + _db_interval(retry_seconds),
            ),
            last_error_code=error_code,
            updated_at=now,
        )
    )


def record_step_failure(
    db: Session,
    job_id: UUID,
    lease_token: UUID,
    *,
    retry_seconds: int,
    error_code: str,
) -> bool:
    result = db.execute(
        build_record_step_failure_statement(
            job_id,
            lease_token,
            retry_seconds=retry_seconds,
            error_code=error_code,
        )
    )
    if result.rowcount != 1:
        return False
    db.execute(
        _build_document_state_sync_statement(
            job_id,
            job_status="delete_failed",
            document_from_status="deleting",
            document_to_status="delete_failed",
        )
    )
    return True


def build_advance_step_statement(
    job_id: UUID,
    lease_token: UUID,
    next_step: str,
) -> Update:
    now = func.now()
    return (
        update(DocumentDeletionJob)
        .where(
            DocumentDeletionJob.id == job_id,
            DocumentDeletionJob.status == "processing",
            DocumentDeletionJob.lease_token == lease_token,
            DocumentDeletionJob.lease_expires_at > now,
        )
        .values(
            status="pending",
            current_step=next_step,
            step_attempts=0,
            locked_by=None,
            lease_token=None,
            locked_at=None,
            lease_expires_at=None,
            next_retry_at=None,
            last_error_code=None,
            updated_at=now,
        )
    )


def build_manual_retry_statement(job_id: UUID) -> Update:
    return (
        update(DocumentDeletionJob)
        .where(
            DocumentDeletionJob.id == job_id,
            DocumentDeletionJob.status == "delete_failed",
        )
        .values(
            status="pending",
            step_attempts=0,
            locked_by=None,
            lease_token=None,
            locked_at=None,
            lease_expires_at=None,
            next_retry_at=None,
            last_error_code=None,
            updated_at=func.now(),
        )
    )


def manual_retry_job(db: Session, job_id: UUID) -> bool:
    result = db.execute(build_manual_retry_statement(job_id))
    if result.rowcount != 1:
        return False
    db.execute(
        _build_document_state_sync_statement(
            job_id,
            job_status="pending",
            document_from_status="delete_failed",
            document_to_status="deleting",
        )
    )
    return True


def _build_document_state_sync_statement(
    job_id: UUID,
    *,
    job_status: str,
    document_from_status: str,
    document_to_status: str,
) -> Update:
    document_id = (
        select(DocumentDeletionJob.document_id)
        .where(
            DocumentDeletionJob.id == job_id,
            DocumentDeletionJob.status == job_status,
        )
        .scalar_subquery()
    )
    return (
        update(Document)
        .where(
            Document.id == document_id,
            Document.deletion_status == document_from_status,
        )
        .values(deletion_status=document_to_status, updated_at=func.now())
    )


def _build_exhausted_document_sync_statement() -> Update:
    failed_document_ids = select(DocumentDeletionJob.document_id).where(
        DocumentDeletionJob.status == "delete_failed",
        DocumentDeletionJob.last_error_code == "DOCUMENT_DELETION_RETRIES_EXHAUSTED",
    )
    return (
        update(Document)
        .where(
            Document.id.in_(failed_document_ids),
            Document.deletion_status == "deleting",
        )
        .values(deletion_status="delete_failed", updated_at=func.now())
    )
