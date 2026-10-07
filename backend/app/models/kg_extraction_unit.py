"""An extraction unit owns one original interval, never a retrieval chunk."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KGExtractionUnit(Base):
    __tablename__ = "kg_extraction_units"
    __table_args__ = (
        ForeignKeyConstraint(
            ["graph_build_id", "document_id", "source_version"],
            ["document_graph_builds.id", "document_graph_builds.document_id", "document_graph_builds.source_version"],
            name="fk_kg_units_build_owner", ondelete="RESTRICT",
        ),
        UniqueConstraint("graph_build_id", "unit_index", name="uq_kg_units_order"),
        UniqueConstraint("graph_build_id", "allocated_anchor_id", name="uq_kg_units_anchor"),
        CheckConstraint("unit_index >= 0 AND source_start >= 0 AND source_end > source_start", name="ck_kg_units_range"),
        CheckConstraint("kind IN ('table', 'clause')", name="ck_kg_units_kind"),
        CheckConstraint(
            "status IN ('pending', 'extracting', 'succeeded_nonempty', 'succeeded_empty', 'failed', 'partial_failed')",
            name="ck_kg_units_status",
        ),
        CheckConstraint(
            "has_qualified_triples = (status = 'succeeded_nonempty')", name="ck_kg_units_qualified",
        ),
        CheckConstraint(
            "length(btrim(allocated_anchor_id)) > 0 AND jsonb_typeof(allocated_anchor_metadata) = 'object' "
            "AND octet_length(allocated_anchor_metadata::text) <= 65536", name="ck_kg_units_anchor_metadata",
        ),
        CheckConstraint(
            "input_sha256 ~ '^[a-f0-9]{64}$' AND (result_sha256 IS NULL OR result_sha256 ~ '^[a-f0-9]{64}$')",
            name="ck_kg_units_hashes",
        ),
        CheckConstraint(
            "(result_object_key IS NULL AND result_sha256 IS NULL) OR "
            "(result_object_key IS NOT NULL AND length(btrim(result_object_key)) > 0 AND result_sha256 IS NOT NULL)",
            name="ck_kg_units_result_pair",
        ),
        CheckConstraint(
            "status NOT IN ('succeeded_nonempty', 'succeeded_empty') OR "
            "(result_object_key IS NOT NULL AND completed_at IS NOT NULL)", name="ck_kg_units_completed",
        ),
        CheckConstraint(
            "jsonb_typeof(piece_checkpoints) = 'object' AND octet_length(piece_checkpoints::text) <= 65536",
            name="ck_kg_units_checkpoints",
        ),
        Index("ix_kg_units_interval", "source_version", "graph_build_id", "source_start", "source_end"),
        Index("ix_kg_units_document", "document_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    graph_build_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_version: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_index: Mapped[int] = mapped_column(Integer, nullable=False)
    allocated_anchor_id: Mapped[str] = mapped_column(String(512), nullable=False)
    allocated_anchor_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    has_qualified_triples: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_object_key: Mapped[str | None] = mapped_column(String(1024))
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    piece_checkpoints: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
