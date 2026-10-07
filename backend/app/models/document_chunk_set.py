"""One independently sealable retrieval segmentation of a frozen source/build."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ChunkSet(Base):
    __tablename__ = "document_chunk_sets"
    __table_args__ = (
        UniqueConstraint("id", "document_id", name="uq_chunk_sets_document"),
        UniqueConstraint("id", "document_id", "source_version", name="uq_chunk_sets_source_owner"),
        UniqueConstraint("id", "document_id", "source_version", "graph_build_id", name="uq_chunk_sets_build_owner"),
        ForeignKeyConstraint(
            ["graph_build_id", "document_id", "source_version"],
            ["document_graph_builds.id", "document_graph_builds.document_id", "document_graph_builds.source_version"],
            name="fk_chunk_sets_build_owner", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('pending', 'chunking', 'chunks_ready', 'embedding', 'indexing', 'indexed', 'failed', 'cancelled')",
            name="ck_chunk_sets_status",
        ),
        CheckConstraint("length(btrim(segmentation_version)) > 0", name="ck_chunk_sets_version"),
        CheckConstraint(
            "jsonb_typeof(segmentation_config) = 'object' AND octet_length(segmentation_config::text) <= 16384",
            name="ck_chunk_sets_config",
        ),
        CheckConstraint(
            "segmentation_config_sha256 ~ '^[a-f0-9]{64}$' AND embedding_fingerprint ~ '^[a-f0-9]{64}$' "
            "AND (manifest_sha256 IS NULL OR manifest_sha256 ~ '^[a-f0-9]{64}$')", name="ck_chunk_sets_hashes",
        ),
        CheckConstraint(
            "(sealed_at IS NULL AND chunk_count IS NULL AND manifest_object_key IS NULL AND manifest_sha256 IS NULL) OR "
            "(sealed_at IS NOT NULL AND chunk_count IS NOT NULL AND chunk_count > 0 AND manifest_object_key IS NOT NULL "
            "AND length(btrim(manifest_object_key)) > 0 AND manifest_sha256 IS NOT NULL)", name="ck_chunk_sets_sealed",
        ),
        CheckConstraint(
            "status NOT IN ('chunks_ready', 'embedding', 'indexing', 'indexed') OR sealed_at IS NOT NULL",
            name="ck_chunk_sets_ready",
        ),
        CheckConstraint(
            "(status = 'indexed' AND indexed_at IS NOT NULL AND index_name IS NOT NULL AND index_receipt IS NOT NULL) "
            "OR (status <> 'indexed' AND indexed_at IS NULL)", name="ck_chunk_sets_indexed",
        ),
        CheckConstraint(
            "(index_name IS NULL OR length(btrim(index_name)) > 0) AND "
            "(index_receipt IS NULL OR (jsonb_typeof(index_receipt) = 'object' "
            "AND octet_length(index_receipt::text) <= 65536))", name="ck_chunk_sets_index_receipt",
        ),
        Index("ix_chunk_sets_document", "document_id", "status"),
        Index("ix_chunk_sets_build", "graph_build_id"),
        Index("ix_chunk_sets_source", "source_version"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_version: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    graph_build_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    segmentation_version: Mapped[str] = mapped_column(String(100), nullable=False)
    segmentation_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    segmentation_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    manifest_object_key: Mapped[str | None] = mapped_column(String(1024))
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    index_name: Mapped[str | None] = mapped_column(String(255))
    index_receipt: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
