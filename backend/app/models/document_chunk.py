from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import VECTOR

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.document_chunk_block import DocumentChunkBlock
    from app.models.document_parse_run import DocumentParseRun
    from app.models.knowledge_item_chunk import KnowledgeItemChunk


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __mapper_args__ = {"eager_defaults": False}
    __table_args__ = (
        Index("ix_document_chunks_document_id", "document_id"),
        Index("ix_document_chunks_document_id_chunk_index", "document_id", "chunk_index"),
        Index("ix_document_chunks_parse_run_id", "parse_run_id"),
        UniqueConstraint("chunk_set_id", "chunk_index", name="uq_document_chunks_set_index"),
        ForeignKeyConstraint(
            ["chunk_set_id", "document_id", "source_version"],
            ["document_chunk_sets.id", "document_chunk_sets.document_id", "document_chunk_sets.source_version"],
            name="fk_document_chunks_set_owner", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_version", "document_id", "parse_run_id"],
            ["document_source_versions.source_version", "document_source_versions.document_id", "document_source_versions.parse_run_id"],
            name="fk_document_chunks_source_parse", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(chunk_set_id IS NULL AND source_version IS NULL AND source_start IS NULL "
            "AND source_end IS NULL AND content_sha256 IS NULL) OR "
            "(chunk_set_id IS NOT NULL AND source_version IS NOT NULL AND source_start IS NOT NULL "
            "AND source_end IS NOT NULL AND content_sha256 IS NOT NULL AND parse_run_id IS NOT NULL "
            "AND source_start >= 0 AND source_end > source_start AND chunk_index >= 0 "
            "AND char_length(content) = source_end - source_start AND content_sha256 ~ '^[a-f0-9]{64}$')",
            name="ck_document_chunks_source_contract",
        ),
        Index("ix_document_chunks_source_interval", "source_version", "source_start", "source_end"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    parse_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("document_parse_runs.id", ondelete="RESTRICT"))
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    section_title: Mapped[str | None] = mapped_column(String(255))
    chunk_type: Mapped[str | None] = mapped_column(String(50))
    chunk_method: Mapped[str | None] = mapped_column(String(50))
    content_format: Mapped[str | None] = mapped_column(String(50))
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Deferred server defaults also keep legacy INSERT/SELECT SQL usable before
    # separately authorized migration. Versioned writers must set all five.
    chunk_set_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), deferred=True, server_default=text("NULL"))
    source_version: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), deferred=True, server_default=text("NULL"))
    source_start: Mapped[int | None] = mapped_column(BigInteger, deferred=True, server_default=text("NULL"))
    source_end: Mapped[int | None] = mapped_column(BigInteger, deferred=True, server_default=text("NULL"))
    content_sha256: Mapped[str | None] = mapped_column(String(64), deferred=True, server_default=text("NULL"))
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(1024), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding_dim: Mapped[int | None] = mapped_column(Integer)
    embedding_status: Mapped[str] = mapped_column(String(50), nullable=False, default="not_started")
    embedding_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    document: Mapped[Document] = relationship("Document", back_populates="chunks")
    parse_run: Mapped[DocumentParseRun | None] = relationship("DocumentParseRun", back_populates="chunks")
    block_mappings: Mapped[list[DocumentChunkBlock]] = relationship(
        "DocumentChunkBlock",
        back_populates="chunk",
    )
    knowledge_item_chunks: Mapped[list[KnowledgeItemChunk]] = relationship(
        "KnowledgeItemChunk",
        back_populates="chunk",
    )
