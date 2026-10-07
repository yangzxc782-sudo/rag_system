"""Persistent graph identity and build outcome; no writer or provider side effects."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GraphBuild(Base):
    __tablename__ = "document_graph_builds"
    __table_args__ = (
        UniqueConstraint("id", "document_id", "source_version", name="uq_graph_builds_source_owner"),
        UniqueConstraint("graph_id", name="uq_graph_builds_graph_id"),
        UniqueConstraint("source_path", name="uq_graph_builds_source_path"),
        ForeignKeyConstraint(
            ["source_version", "document_id"],
            ["document_source_versions.source_version", "document_source_versions.document_id"],
            name="fk_graph_builds_source_owner", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('pending', 'extracting', 'extraction_failed', 'partial_failed', "
            "'writing', 'write_failed', 'ready', 'ready_empty', 'cancelled')", name="ck_graph_builds_status",
        ),
        CheckConstraint("graph_schema_version = 2", name="ck_graph_builds_schema"),
        CheckConstraint(
            "length(btrim(graph_id)) > 0 AND length(btrim(source_path)) > 0 "
            "AND length(btrim(template_version)) > 0 AND length(btrim(unit_rule_version)) > 0",
            name="ck_graph_builds_fields",
        ),
        CheckConstraint(
            "template_sha256 ~ '^[a-f0-9]{64}$' AND unit_rule_sha256 ~ '^[a-f0-9]{64}$' "
            "AND provider_fingerprint ~ '^[a-f0-9]{64}$' AND input_fingerprint ~ '^[a-f0-9]{64}$' "
            "AND (result_sha256 IS NULL OR result_sha256 ~ '^[a-f0-9]{64}$')", name="ck_graph_builds_hashes",
        ),
        CheckConstraint(
            "(result_object_key IS NULL AND result_sha256 IS NULL) OR "
            "(result_object_key IS NOT NULL AND length(btrim(result_object_key)) > 0 AND result_sha256 IS NOT NULL)",
            name="ck_graph_builds_result_pair",
        ),
        CheckConstraint(
            "unit_count >= 0 AND anchor_count >= 0 AND entity_count >= 0 AND relationship_count >= 0 "
            "AND anchor_count <= unit_count", name="ck_graph_builds_counts",
        ),
        CheckConstraint(
            "(status IN ('ready', 'ready_empty') AND sealed_at IS NOT NULL AND result_object_key IS NOT NULL "
            "AND unit_count IS NOT NULL AND anchor_count IS NOT NULL AND entity_count IS NOT NULL "
            "AND relationship_count IS NOT NULL) OR "
            "(status NOT IN ('ready', 'ready_empty') AND sealed_at IS NULL)", name="ck_graph_builds_sealed",
        ),
        CheckConstraint(
            "(status <> 'ready' OR (anchor_count > 0 AND entity_count > 0 AND relationship_count > 0)) "
            "AND (status <> 'ready_empty' OR (anchor_count = 0 AND entity_count = 0 AND relationship_count = 0))",
            name="ck_graph_builds_empty",
        ),
        CheckConstraint(
            "jsonb_typeof(write_checkpoint) = 'object' AND octet_length(write_checkpoint::text) <= 65536",
            name="ck_graph_builds_checkpoint",
        ),
        Index("ix_graph_builds_source", "source_version", "status"),
        Index("ix_graph_builds_document", "document_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_version: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    graph_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_day: Mapped[date] = mapped_column(Date, nullable=False)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    graph_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    template_version: Mapped[str] = mapped_column(String(100), nullable=False)
    template_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    unit_rule_version: Mapped[str] = mapped_column(String(100), nullable=False)
    unit_rule_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    result_object_key: Mapped[str | None] = mapped_column(String(1024))
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    unit_count: Mapped[int | None] = mapped_column(Integer)
    anchor_count: Mapped[int | None] = mapped_column(Integer)
    entity_count: Mapped[int | None] = mapped_column(Integer)
    relationship_count: Mapped[int | None] = mapped_column(Integer)
    write_checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
