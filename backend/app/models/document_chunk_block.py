from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.document_block import DocumentBlock
    from app.models.document_chunk import DocumentChunk


class DocumentChunkBlock(Base):
    __tablename__ = "document_chunk_blocks"
    __table_args__ = (
        Index("ix_document_chunk_blocks_chunk_id", "chunk_id"),
        Index("ix_document_chunk_blocks_block_id", "block_id"),
        UniqueConstraint("chunk_id", "block_order", name="uq_document_chunk_blocks_chunk_order"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    chunk_id: Mapped[UUID] = mapped_column(ForeignKey("document_chunks.id", ondelete="RESTRICT"), nullable=False)
    block_id: Mapped[UUID] = mapped_column(ForeignKey("document_blocks.id", ondelete="RESTRICT"), nullable=False)
    block_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    chunk: Mapped[DocumentChunk] = relationship("DocumentChunk", back_populates="block_mappings")
    block: Mapped[DocumentBlock] = relationship("DocumentBlock", back_populates="chunk_mappings")
