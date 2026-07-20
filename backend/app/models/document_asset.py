from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.document_parse_run import DocumentParseRun


class DocumentAsset(Base):
    __tablename__ = "document_assets"
    __table_args__ = (
        Index("ix_document_assets_parse_run_id", "parse_run_id"),
        Index("ix_document_assets_document_id", "document_id"),
        Index("ix_document_assets_asset_type", "asset_type"),
        UniqueConstraint("parse_run_id", "asset_key", name="uq_document_assets_parse_run_asset_key"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False)
    parse_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_parse_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_type: Mapped[str] = mapped_column(String(50), nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    asset_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    caption: Mapped[str | None] = mapped_column(Text)
    source_block_key: Mapped[str | None] = mapped_column(String(255))
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    document: Mapped[Document] = relationship("Document", back_populates="assets")
    parse_run: Mapped[DocumentParseRun] = relationship("DocumentParseRun", back_populates="assets")
