from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.knowledge_entry import KnowledgeEntry


class EntryReviewRecord(Base):
    __tablename__ = "entry_review_records"
    __table_args__ = (Index("ix_entry_review_records_entry_id", "entry_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    entry_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_entries.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(255))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    entry: Mapped[KnowledgeEntry] = relationship("KnowledgeEntry", back_populates="review_records")
