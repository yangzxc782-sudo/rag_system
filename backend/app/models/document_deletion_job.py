from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


DOCUMENT_DELETION_JOB_STATUSES = ("pending", "processing", "retry_wait", "delete_failed")
DOCUMENT_DELETION_STEPS = (
    "delete_opensearch",
    "delete_minio_derived",
    "delete_minio_raw",
    "finalize_postgresql",
)


class DocumentDeletionJob(Base):
    __tablename__ = "document_deletion_jobs"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_document_deletion_jobs_document_id"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'retry_wait', 'delete_failed')",
            name="ck_document_deletion_jobs_status",
        ),
        CheckConstraint(
            "current_step IN ('delete_opensearch', 'delete_minio_derived', "
            "'delete_minio_raw', 'finalize_postgresql')",
            name="ck_document_deletion_jobs_current_step",
        ),
        CheckConstraint("step_attempts >= 0", name="ck_document_deletion_jobs_step_attempts"),
        CheckConstraint("max_attempts > 0", name="ck_document_deletion_jobs_max_attempts"),
        CheckConstraint(
            "(status = 'processing' AND locked_by IS NOT NULL AND lease_token IS NOT NULL "
            "AND locked_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND locked_by IS NULL AND lease_token IS NULL "
            "AND locked_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_document_deletion_jobs_lease_fields",
        ),
        CheckConstraint(
            "(status = 'retry_wait' AND next_retry_at IS NOT NULL) OR "
            "(status <> 'retry_wait' AND next_retry_at IS NULL)",
            name="ck_document_deletion_jobs_retry_fields",
        ),
        Index(
            "ix_document_deletion_jobs_claimable",
            "status",
            "next_retry_at",
            "lease_expires_at",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    current_step: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="delete_opensearch",
        server_default="delete_opensearch",
    )
    step_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    locked_by: Mapped[str | None] = mapped_column(String(255))
    lease_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

