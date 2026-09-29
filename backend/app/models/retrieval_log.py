from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.qa_message import QAMessage
    from app.models.qa_session import QASession


class RetrievalLog(Base):
    __tablename__ = "retrieval_logs"
    __table_args__ = (
        Index("ix_retrieval_logs_session_id", "session_id"),
        Index("ix_retrieval_logs_message_id", "message_id"),
        UniqueConstraint("turn_id", "evidence_generation", name="uq_retrieval_logs_turn_generation"),
        ForeignKeyConstraint(
            ["session_id", "turn_id"], ["qa_turns.session_id", "qa_turns.id"],
            name="fk_retrieval_logs_turn_session",
        ),
        ForeignKeyConstraint(
            ["session_id", "message_id"], ["qa_messages.session_id", "qa_messages.id"],
            name="fk_retrieval_logs_message_session",
        ),
        ForeignKeyConstraint(
            ["session_id", "turn_id", "message_id"],
            ["qa_messages.session_id", "qa_messages.turn_id", "qa_messages.id"],
            name="fk_retrieval_logs_message_turn",
        ),
        CheckConstraint(
            "(turn_id IS NULL AND evidence_generation IS NULL) OR "
            "(turn_id IS NOT NULL AND evidence_generation > 0 AND evidence_generation IS NOT NULL AND message_id IS NOT NULL)",
            name="ck_retrieval_logs_turn_generation",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("qa_sessions.id"), nullable=False)
    message_id: Mapped[UUID | None] = mapped_column(ForeignKey("qa_messages.id"))
    turn_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    evidence_generation: Mapped[int | None] = mapped_column(Integer)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_type: Mapped[str | None] = mapped_column(String(100))
    top_k: Mapped[int | None] = mapped_column(Integer)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session: Mapped[QASession] = relationship("QASession", back_populates="retrieval_logs")
    message: Mapped[QAMessage | None] = relationship(
        "QAMessage", back_populates="retrieval_logs", foreign_keys=[message_id],
    )
