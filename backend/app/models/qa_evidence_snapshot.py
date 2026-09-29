from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QAEvidenceSnapshot(Base):
    __tablename__ = "qa_evidence_snapshots"
    __table_args__ = (
        UniqueConstraint("session_id", "turn_id", "id", name="uq_qa_snapshots_turn_identity"),
        UniqueConstraint("artifact_id", "snapshot_key", name="uq_qa_snapshots_key"),
        ForeignKeyConstraint(
            ["session_id", "turn_id", "artifact_id"],
            ["qa_turn_artifacts.session_id", "qa_turn_artifacts.turn_id", "qa_turn_artifacts.id"],
            name="fk_qa_snapshots_artifact_turn",
        ),
        CheckConstraint("schema_version > 0", name="ck_qa_snapshots_version"),
        CheckConstraint("kind IN ('candidate', 'citation', 'graph', 'answer_draft')", name="ck_qa_snapshots_kind"),
        CheckConstraint(
            "(status = 'available' AND payload IS NOT NULL AND redacted_at IS NULL AND redacted_document_id IS NULL) OR "
            "(status = 'source_deleted' AND payload IS NULL AND redacted_at IS NOT NULL AND redacted_document_id IS NOT NULL)",
            name="ck_qa_snapshots_redaction",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    snapshot_key: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="available")
    redacted_document_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
