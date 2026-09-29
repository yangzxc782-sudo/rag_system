from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QAEvidenceSource(Base):
    __tablename__ = "qa_evidence_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "turn_id", "snapshot_id"],
            ["qa_evidence_snapshots.session_id", "qa_evidence_snapshots.turn_id", "qa_evidence_snapshots.id"],
            name="fk_qa_sources_snapshot_turn",
        ),
        Index("ix_qa_sources_document", "document_id", "snapshot_id"),
        Index("ix_qa_sources_snapshot", "snapshot_id"),
        Index("uq_qa_sources_document_only", "snapshot_id", "document_id", unique=True, postgresql_where=text("chunk_id IS NULL")),
        Index("uq_qa_sources_chunk", "snapshot_id", "chunk_id", unique=True, postgresql_where=text("chunk_id IS NOT NULL")),
        CheckConstraint(
            "(status = 'available' AND deleted_at IS NULL) OR "
            "(status = 'source_deleted' AND deleted_at IS NOT NULL)",
            name="ck_qa_sources_deletion",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    snapshot_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # Immutable provenance, intentionally no FK to removable Documents/Chunks.
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    chunk_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="available")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
