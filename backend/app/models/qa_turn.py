from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QATurn(Base):
    __tablename__ = "qa_turns"
    __mapper_args__ = {"eager_defaults": False}
    __table_args__ = (
        ForeignKeyConstraint(["session_id", "requested_casting_input_file_id"],
                             ["casting_design_files.session_id", "casting_design_files.id"],
                             name="fk_qa_turns_requested_casting", use_alter=True),
        ForeignKeyConstraint(["session_id", "effective_casting_input_file_id"],
                             ["casting_design_files.session_id", "casting_design_files.id"],
                             name="fk_qa_turns_effective_casting", use_alter=True),
        UniqueConstraint("session_id", "id", name="uq_qa_turns_session_id"),
        UniqueConstraint("session_id", "request_id", name="uq_qa_turns_request"),
        UniqueConstraint("session_id", "turn_no", name="uq_qa_turns_number"),
        Index("uq_qa_turns_unresolved_session", "session_id", unique=True,
              postgresql_where=text("status IN ('running', 'finalizing', 'needs_recovery')")),
        CheckConstraint("turn_no > 0 AND attempt_no > 0", name="ck_qa_turns_numbers"),
        CheckConstraint("retrieval_limit BETWEEN 1 AND 50", name="ck_qa_turns_limit"),
        CheckConstraint("status IN ('running', 'finalizing', 'completed', 'failed', 'needs_recovery')", name="ck_qa_turns_status"),
        CheckConstraint("outcome IS NULL OR outcome IN ('answer', 'no_context', 'clarification', 'casting_design')", name="ck_qa_turns_outcome"),
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL AND outcome IS NOT NULL AND error_code IS NULL) OR "
            "(status <> 'completed' AND completed_at IS NULL AND outcome IS NULL)",
            name="ck_qa_turns_completion",
        ),
        CheckConstraint(
            "(status IN ('failed', 'needs_recovery') AND error_code IS NOT NULL) OR "
            "(status NOT IN ('failed', 'needs_recovery') AND error_code IS NULL)",
            name="ck_qa_turns_error",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("qa_sessions.id"), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    turn_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    # Request parameter, not a retaining FK to a removable source document.
    document_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # Deferred and omitted from legacy INSERTs so disabled casting remains usable
    # on 0010. Only the explicitly enabled casting layer reads/writes these fields.
    graph_version: Mapped[str | None] = mapped_column(String(64), deferred=True, server_default=text("NULL"))
    requested_casting_input_file_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), deferred=True, server_default=text("NULL"))
    effective_casting_input_file_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), deferred=True, server_default=text("NULL"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="running")
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    outcome: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
