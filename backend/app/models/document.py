from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.document_asset import DocumentAsset
    from app.models.document_block import DocumentBlock
    from app.models.document_chunk import DocumentChunk
    from app.models.document_parse_run import DocumentParseRun
    from app.models.knowledge_item import KnowledgeItem
    from app.models.knowledge_item_chunk import KnowledgeItemChunk
    from app.models.knowledge_item_source import KnowledgeItemSource


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "deletion_status IN ('normal', 'deleting', 'delete_failed')",
            name="ck_documents_deletion_status",
        ),
        Index("ix_documents_deletion_status", "deletion_status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    bucket_name: Mapped[str] = mapped_column(String(255), nullable=False, default="rag-documents")
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    file_type: Mapped[str | None] = mapped_column(String(100))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    file_hash: Mapped[str | None] = mapped_column(String(64))
    process_status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    deletion_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="normal",
        server_default=text("'normal'"),
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    chunks: Mapped[list[DocumentChunk]] = relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    parse_runs: Mapped[list[DocumentParseRun]] = relationship("DocumentParseRun", back_populates="document")
    blocks: Mapped[list[DocumentBlock]] = relationship("DocumentBlock", back_populates="document")
    assets: Mapped[list[DocumentAsset]] = relationship("DocumentAsset", back_populates="document")
    knowledge_items: Mapped[list[KnowledgeItem]] = relationship("KnowledgeItem", back_populates="source_document")
    knowledge_item_chunks: Mapped[list[KnowledgeItemChunk]] = relationship(
        "KnowledgeItemChunk",
        back_populates="document",
    )
    knowledge_item_sources: Mapped[list[KnowledgeItemSource]] = relationship(
        "KnowledgeItemSource",
        back_populates="document",
        cascade="save-update, merge",
        passive_deletes="all",
    )
