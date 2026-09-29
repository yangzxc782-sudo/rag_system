from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.qa_session import QASession
    from app.models.retrieval_log import RetrievalLog


class QAMessage(Base):
    __tablename__ = "qa_messages"
    __table_args__ = (
        Index("ix_qa_messages_session_id", "session_id"),
        UniqueConstraint("session_id", "sequence_no", name="uq_qa_messages_sequence"),
        UniqueConstraint("turn_id", "role", name="uq_qa_messages_turn_role"),
        UniqueConstraint("session_id", "id", name="uq_qa_messages_session_id"),
        UniqueConstraint("session_id", "turn_id", "id", name="uq_qa_messages_turn_identity"),
        ForeignKeyConstraint(
            ["session_id", "turn_id"], ["qa_turns.session_id", "qa_turns.id"],
            name="fk_qa_messages_turn_session",
        ),
        ForeignKeyConstraint(
            ["session_id", "turn_id", "answer_snapshot_id"],
            ["qa_evidence_snapshots.session_id", "qa_evidence_snapshots.turn_id", "qa_evidence_snapshots.id"],
            name="fk_qa_messages_answer_turn",
        ),
        CheckConstraint(
            "(turn_id IS NULL AND answer_snapshot_id IS NULL) OR "
            "(turn_id IS NOT NULL AND ((role = 'user' AND answer_snapshot_id IS NULL) OR "
            "(role = 'assistant' AND answer_snapshot_id IS NOT NULL)))",
            name="ck_qa_messages_answer_reference",
        ),
        CheckConstraint("sequence_no > 0", name="ck_qa_messages_sequence"),
        CheckConstraint("turn_id IS NULL OR role IN ('user', 'assistant')", name="ck_qa_messages_turn_role"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("qa_sessions.id"), nullable=False)
    turn_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    answer_snapshot_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session: Mapped[QASession] = relationship("QASession", back_populates="messages")
    retrieval_logs: Mapped[list[RetrievalLog]] = relationship(
        "RetrievalLog",
        back_populates="message",
        foreign_keys="RetrievalLog.message_id",
    )
