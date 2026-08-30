from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.knowledge_item_chunk import KnowledgeItemChunk
    from app.models.knowledge_item_review import KnowledgeItemReview
    from app.models.knowledge_item_source import KnowledgeItemSource
    from app.models.knowledge_item_version import KnowledgeItemVersion


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"
    __table_args__ = (
        Index("ix_knowledge_items_status", "status"),
        Index("ix_knowledge_items_item_type", "item_type"),
        Index("ix_knowledge_items_source_document_id", "source_document_id"),
        Index("ix_knowledge_items_content_hash", "content_hash"),
        Index("ix_knowledge_items_created_at", "created_at"),
        Index("ix_knowledge_items_revises_item_id", "revises_item_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    item_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    structured_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    entities: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    conditions: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="draft")
    source_document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id"))
    source_filename: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_comment: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    revises_item_id: Mapped[UUID | None] = mapped_column(ForeignKey("knowledge_items.id"))

    source_document: Mapped[Document | None] = relationship("Document", back_populates="knowledge_items")
    chunks: Mapped[list[KnowledgeItemChunk]] = relationship(
        "KnowledgeItemChunk",
        back_populates="knowledge_item",
        cascade="all, delete-orphan",
    )
    reviews: Mapped[list[KnowledgeItemReview]] = relationship(
        "KnowledgeItemReview",
        back_populates="knowledge_item",
        cascade="all, delete-orphan",
    )
    versions: Mapped[list[KnowledgeItemVersion]] = relationship(
        "KnowledgeItemVersion",
        back_populates="knowledge_item",
        cascade="all, delete-orphan",
    )
    sources: Mapped[list[KnowledgeItemSource]] = relationship(
        "KnowledgeItemSource",
        back_populates="knowledge_item",
        cascade="save-update, merge",
        order_by="(KnowledgeItemSource.created_at, KnowledgeItemSource.document_id)",
        passive_deletes="all",
    )
    revises_item: Mapped[KnowledgeItem | None] = relationship(
        "KnowledgeItem",
        remote_side=[id],
        back_populates="revisions",
    )
    revisions: Mapped[list[KnowledgeItem]] = relationship(
        "KnowledgeItem",
        back_populates="revises_item",
    )
