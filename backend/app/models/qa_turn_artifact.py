from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QATurnArtifact(Base):
    __tablename__ = "qa_turn_artifacts"
    __table_args__ = (
        UniqueConstraint("session_id", "turn_id", "id", name="uq_qa_artifacts_turn_identity"),
        UniqueConstraint("turn_id", "attempt_no", "artifact_key", name="uq_qa_artifacts_key"),
        ForeignKeyConstraint(["session_id", "turn_id"], ["qa_turns.session_id", "qa_turns.id"], name="fk_qa_artifacts_turn_session"),
        ForeignKeyConstraint(
            ["session_id", "turn_id", "parent_artifact_id"],
            ["qa_turn_artifacts.session_id", "qa_turn_artifacts.turn_id", "qa_turn_artifacts.id"],
            name="fk_qa_artifacts_parent_turn",
        ),
        CheckConstraint("attempt_no > 0 AND schema_version > 0", name="ck_qa_artifacts_versions"),
        CheckConstraint("kind IN ('context', 'rewrite', 'retrieval', 'evidence', 'generation', 'result')", name="ck_qa_artifacts_kind"),
        CheckConstraint("parent_artifact_id IS NULL OR parent_artifact_id <> id", name="ck_qa_artifacts_parent"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_artifact_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # Validated with PersistenceMetrics; evidence is stored only in snapshots.
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
