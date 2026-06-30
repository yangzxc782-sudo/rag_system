from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.qa_message import QAMessage
    from app.models.retrieval_log import RetrievalLog


class QASession(Base):
    __tablename__ = "qa_sessions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str | None] = mapped_column(String(255))
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
