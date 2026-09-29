from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.qa_message import QAMessage
    from app.models.retrieval_log import RetrievalLog


class QASession(Base):
    __tablename__ = "qa_sessions"
    __table_args__ = (
        UniqueConstraint("create_request_id", name="uq_qa_sessions_create_request"),
        CheckConstraint("next_turn_no > 0 AND next_message_seq > 0", name="ck_qa_sessions_counters"),
        CheckConstraint(
            "(create_request_id IS NULL AND create_fingerprint IS NULL) OR "
            "(create_request_id IS NOT NULL AND create_fingerprint IS NOT NULL)",
            name="ck_qa_sessions_creation_identity",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str | None] = mapped_column(String(255))
    create_request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    create_fingerprint: Mapped[str | None] = mapped_column(String(64))
    next_turn_no: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    next_message_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    messages: Mapped[list[QAMessage]] = relationship(
        "QAMessage",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    retrieval_logs: Mapped[list[RetrievalLog]] = relationship(
        "RetrievalLog",
        back_populates="session",
        cascade="all, delete-orphan",
    )
