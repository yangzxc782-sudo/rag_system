from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk
    from app.models.entry_review_record import EntryReviewRecord
    from app.models.entry_version import EntryVersion


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"
    __table_args__ = (
        Index("ix_knowledge_entries_source_document_id", "source_document_id"),
        Index("ix_knowledge_entries_source_chunk_id", "source_chunk_id"),
        Index("ix_knowledge_entries_review_status", "review_status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    entry_type: Mapped[str | None] = mapped_column(String(100))
    subject: Mapped[str | None] = mapped_column(String(255))
    condition: Mapped[str | None] = mapped_column(Text)
    conclusion: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    source_chunk_id: Mapped[UUID] = mapped_column(ForeignKey("document_chunks.id"), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    review_status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending_review")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding_dim: Mapped[int | None] = mapped_column(Integer)
    embedding_status: Mapped[str] = mapped_column(String(50), nullable=False, default="not_started")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    source_document: Mapped[Document] = relationship("Document", back_populates="knowledge_entries")
    source_chunk: Mapped[DocumentChunk] = relationship(
        "DocumentChunk",
        back_populates="knowledge_entries",
    )
    versions: Mapped[list[EntryVersion]] = relationship(
        "EntryVersion",
        back_populates="entry",
        cascade="all, delete-orphan",
    )
    review_records: Mapped[list[EntryReviewRecord]] = relationship(
        "EntryReviewRecord",
        back_populates="entry",
        cascade="all, delete-orphan",
    )
