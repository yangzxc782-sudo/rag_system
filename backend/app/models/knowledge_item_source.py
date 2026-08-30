from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.knowledge_item import KnowledgeItem


class KnowledgeItemSource(Base):
    """Normalized source fact; filename is only a provenance snapshot."""

    __tablename__ = "knowledge_item_sources"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_item_id",
            "document_id",
            name="uq_knowledge_item_sources_item_document",
        ),
        Index("ix_knowledge_item_sources_knowledge_item_id", "knowledge_item_id"),
        Index("ix_knowledge_item_sources_document_id", "document_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    knowledge_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_filename: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    knowledge_item: Mapped[KnowledgeItem] = relationship("KnowledgeItem", back_populates="sources")
    document: Mapped[Document] = relationship("Document", back_populates="knowledge_item_sources")

