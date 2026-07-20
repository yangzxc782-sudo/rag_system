from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.document_chunk_block import DocumentChunkBlock
    from app.models.document_parse_run import DocumentParseRun


class DocumentBlock(Base):
    __tablename__ = "document_blocks"
    __table_args__ = (
        Index("ix_document_blocks_parse_run_id", "parse_run_id"),
        Index("ix_document_blocks_document_id", "document_id"),
        Index("ix_document_blocks_block_type", "block_type"),
        UniqueConstraint("parse_run_id", "block_index", name="uq_document_blocks_parse_run_block_index"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False)
    parse_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_parse_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    block_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_key: Mapped[str | None] = mapped_column(String(255))
    block_type: Mapped[str] = mapped_column(String(50), nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    text: Mapped[str | None] = mapped_column(Text)
    markdown: Mapped[str | None] = mapped_column(Text)
    html: Mapped[str | None] = mapped_column(Text)
    latex: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    parent_block_key: Mapped[str | None] = mapped_column(String(255))
    section_path: Mapped[list[Any] | dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    document: Mapped[Document] = relationship("Document", back_populates="blocks")
    parse_run: Mapped[DocumentParseRun] = relationship("DocumentParseRun", back_populates="blocks")
    chunk_mappings: Mapped[list[DocumentChunkBlock]] = relationship(
        "DocumentChunkBlock",
        back_populates="block",
    )
