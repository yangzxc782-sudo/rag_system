from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk
    from app.models.knowledge_item import KnowledgeItem


class KnowledgeItemChunk(Base):
    __tablename__ = "knowledge_item_chunks"
    __table_args__ = (
        Index("ix_knowledge_item_chunks_knowledge_item_id", "knowledge_item_id"),
        Index("ix_knowledge_item_chunks_chunk_id", "chunk_id"),
        Index("ix_knowledge_item_chunks_document_id", "document_id"),
        UniqueConstraint("knowledge_item_id", "chunk_id", name="uq_knowledge_item_chunks_item_chunk"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    knowledge_item_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_items.id"), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(ForeignKey("document_chunks.id"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    knowledge_item: Mapped[KnowledgeItem] = relationship("KnowledgeItem", back_populates="chunks")
    document: Mapped[Document] = relationship("Document", back_populates="knowledge_item_chunks")
    chunk: Mapped[DocumentChunk] = relationship("DocumentChunk", back_populates="knowledge_item_chunks")
