from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.document_asset import DocumentAsset
    from app.models.document_block import DocumentBlock
    from app.models.document_chunk import DocumentChunk


class DocumentParseRun(Base):
    __tablename__ = "document_parse_runs"
    __table_args__ = (
        UniqueConstraint("id", "document_id", name="uq_document_parse_runs_owner"),
        Index("ix_document_parse_runs_document_id", "document_id"),
        Index("ix_document_parse_runs_status", "status"),
        Index("ix_document_parse_runs_is_active", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False)
    parser_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(100))
    parse_mode: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    input_file_key: Mapped[str | None] = mapped_column(String(1024))
    output_prefix: Mapped[str | None] = mapped_column(String(1024))
    output_markdown_key: Mapped[str | None] = mapped_column(String(1024))
    output_json_key: Mapped[str | None] = mapped_column(String(1024))
    page_count: Mapped[int | None] = mapped_column(Integer)
    block_count: Mapped[int | None] = mapped_column(Integer)
    asset_count: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    document: Mapped[Document] = relationship("Document", back_populates="parse_runs")
    blocks: Mapped[list[DocumentBlock]] = relationship("DocumentBlock", back_populates="parse_run")
    assets: Mapped[list[DocumentAsset]] = relationship("DocumentAsset", back_populates="parse_run")
    chunks: Mapped[list[DocumentChunk]] = relationship("DocumentChunk", back_populates="parse_run")
