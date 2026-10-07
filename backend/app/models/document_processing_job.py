"""Durable PDF processing and re-chunk jobs, executed by the M5 worker."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentProcessingJob(Base):
    __tablename__ = "document_processing_jobs"
    __table_args__ = (
        UniqueConstraint("document_id", "operation", "request_id", name="uq_processing_jobs_request"),
        ForeignKeyConstraint(
            ["source_version", "document_id"],
            ["document_source_versions.source_version", "document_source_versions.document_id"],
            name="fk_processing_jobs_source_owner", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["graph_build_id", "document_id", "source_version"],
            ["document_graph_builds.id", "document_graph_builds.document_id", "document_graph_builds.source_version"],
            name="fk_processing_jobs_build_owner", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["chunk_set_id", "document_id", "source_version", "graph_build_id"],
            ["document_chunk_sets.id", "document_chunk_sets.document_id", "document_chunk_sets.source_version", "document_chunk_sets.graph_build_id"],
            name="fk_processing_jobs_set_owner", ondelete="RESTRICT",
        ),
        CheckConstraint("operation IN ('process', 'rechunk')", name="ck_processing_jobs_operation"),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled')",
            name="ck_processing_jobs_status",
        ),
        CheckConstraint(
            "stage IN ('uploaded', 'parsing', 'parsed', 'cleaning', 'source_ready', 'kg_extracting', "
            "'kg_writing', 'kg_ready', 'chunking', 'chunks_ready', 'embedding', 'indexing', 'indexed')",
            name="ck_processing_jobs_stage",
        ),
        CheckConstraint(
            "(graph_build_id IS NULL OR source_version IS NOT NULL) AND "
            "(chunk_set_id IS NULL OR (graph_build_id IS NOT NULL AND source_version IS NOT NULL)) AND "
            "(operation <> 'rechunk' OR (source_version IS NOT NULL AND graph_build_id IS NOT NULL "
            "AND stage IN ('kg_ready', 'chunking', 'chunks_ready', 'embedding', 'indexing', 'indexed')))",
            name="ck_processing_jobs_versions",
        ),
        CheckConstraint("input_fingerprint ~ '^[a-f0-9]{64}$'", name="ck_processing_jobs_input"),
        CheckConstraint(
            "jsonb_typeof(checkpoint) = 'object' AND octet_length(checkpoint::text) <= 65536",
            name="ck_processing_jobs_checkpoint",
        ),
        CheckConstraint(
            "fencing_token >= 0 AND attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
            name="ck_processing_jobs_attempts",
        ),
        CheckConstraint(
            "(status = 'running' AND locked_by IS NOT NULL AND length(btrim(locked_by)) > 0 "
            "AND lease_token IS NOT NULL AND locked_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at > locked_at AND fencing_token > 0 AND attempt_count > 0) OR "
            "(status <> 'running' AND locked_by IS NULL AND lease_token IS NULL "
            "AND locked_at IS NULL AND lease_expires_at IS NULL)", name="ck_processing_jobs_lease",
        ),
        CheckConstraint(
            "(status = 'retry_wait' AND next_retry_at IS NOT NULL) OR "
            "(status <> 'retry_wait' AND next_retry_at IS NULL)", name="ck_processing_jobs_retry",
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NOT NULL) OR "
            "(status NOT IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NULL)",
            name="ck_processing_jobs_finished",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR (stage = 'indexed' AND chunk_set_id IS NOT NULL)",
            name="ck_processing_jobs_success",
        ),
        CheckConstraint(
            "status NOT IN ('failed', 'retry_wait') OR "
            "(last_error_code IS NOT NULL AND length(btrim(last_error_code)) > 0)", name="ck_processing_jobs_error",
        ),
        Index(
            "uq_processing_jobs_active_document", "document_id", unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'retry_wait')"),
        ),
        Index("ix_processing_jobs_claimable", "status", "next_retry_at", "lease_expires_at", "created_at"),
        Index("ix_processing_jobs_source", "source_version"),
        Index("ix_processing_jobs_build", "graph_build_id"),
        Index("ix_processing_jobs_set", "chunk_set_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    graph_build_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    chunk_set_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    locked_by: Mapped[str | None] = mapped_column(String(255))
    lease_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
