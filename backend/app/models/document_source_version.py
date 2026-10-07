"""A frozen canonical source, independent of parse attempts and retrieval chunks."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SourceDocumentVersion(Base):
    __tablename__ = "document_source_versions"
    __table_args__ = (
        UniqueConstraint("source_version", "document_id", name="uq_source_versions_document"),
        UniqueConstraint("source_version", "document_id", "parse_run_id", name="uq_source_versions_parse"),
        ForeignKeyConstraint(
            ["parse_run_id", "document_id"], ["document_parse_runs.id", "document_parse_runs.document_id"],
            name="fk_source_versions_parse_owner", ondelete="RESTRICT",
        ),
        CheckConstraint("character_count > 0", name="ck_source_versions_length"),
        CheckConstraint(
            "canonical_sha256 ~ '^[a-f0-9]{64}$' AND block_map_sha256 ~ '^[a-f0-9]{64}$' "
            "AND cleaning_config_sha256 ~ '^[a-f0-9]{64}$'", name="ck_source_versions_hashes",
        ),
        CheckConstraint(
            "length(btrim(bucket_name)) > 0 AND length(btrim(canonical_object_key)) > 0 "
            "AND length(btrim(block_map_object_key)) > 0 AND length(btrim(cleaner_version)) > 0 "
            "AND length(btrim(renderer_version)) > 0", name="ck_source_versions_fields",
        ),
        CheckConstraint(
            "coordinate_unit = 'unicode_code_point' AND normalization = 'LF_NFC_UTF8_v1'",
            name="ck_source_versions_coordinates",
        ),
        UniqueConstraint("bucket_name", "canonical_object_key", name="uq_source_versions_object"),
        Index("ix_source_versions_document", "document_id"),
        Index("ix_source_versions_parse", "parse_run_id"),
    )

    source_version: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    parse_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    bucket_name: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    character_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_map_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    block_map_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cleaner_version: Mapped[str] = mapped_column(String(100), nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(100), nullable=False)
    cleaning_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    coordinate_unit: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unicode_code_point", server_default="unicode_code_point",
    )
    normalization: Mapped[str] = mapped_column(
        String(32), nullable=False, default="LF_NFC_UTF8_v1", server_default="LF_NFC_UTF8_v1",
    )
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
