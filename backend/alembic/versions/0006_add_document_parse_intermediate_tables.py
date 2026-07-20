"""add document parse intermediate tables

Revision ID: 0006_add_document_parse
Revises: 0005_knowledge_items
Create Date: 2026-07-08 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0006_add_document_parse"
down_revision: str | None = "0005_knowledge_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_parse_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parser_provider", sa.String(length=50), nullable=False),
        sa.Column("parser_version", sa.String(length=100), nullable=True),
        sa.Column("parse_mode", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("input_file_key", sa.String(length=1024), nullable=True),
        sa.Column("output_prefix", sa.String(length=1024), nullable=True),
        sa.Column("output_markdown_key", sa.String(length=1024), nullable=True),
        sa.Column("output_json_key", sa.String(length=1024), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("block_count", sa.Integer(), nullable=True),
        sa.Column("asset_count", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("source_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_parse_runs_document_id", "document_parse_runs", ["document_id"])
    op.create_index("ix_document_parse_runs_is_active", "document_parse_runs", ["is_active"])
    op.create_index("ix_document_parse_runs_status", "document_parse_runs", ["status"])

    op.create_table(
        "document_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parse_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("block_index", sa.Integer(), nullable=False),
        sa.Column("block_key", sa.String(length=255), nullable=True),
        sa.Column("block_type", sa.String(length=50), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("bbox", postgresql.JSONB(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("markdown", sa.Text(), nullable=True),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("latex", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("parent_block_key", sa.String(length=255), nullable=True),
        sa.Column("section_path", postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("source_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parse_run_id"], ["document_parse_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parse_run_id", "block_index", name="uq_document_blocks_parse_run_block_index"),
    )
    op.create_index("ix_document_blocks_block_type", "document_blocks", ["block_type"])
    op.create_index("ix_document_blocks_document_id", "document_blocks", ["document_id"])
    op.create_index("ix_document_blocks_parse_run_id", "document_blocks", ["parse_run_id"])

    op.create_table(
        "document_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parse_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_type", sa.String(length=50), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("asset_key", sa.String(length=1024), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("source_block_key", sa.String(length=255), nullable=True),
        sa.Column("source_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parse_run_id"], ["document_parse_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parse_run_id", "asset_key", name="uq_document_assets_parse_run_asset_key"),
    )
    op.create_index("ix_document_assets_asset_type", "document_assets", ["asset_type"])
    op.create_index("ix_document_assets_document_id", "document_assets", ["document_id"])
    op.create_index("ix_document_assets_parse_run_id", "document_assets", ["parse_run_id"])

    op.add_column("document_chunks", sa.Column("parse_run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("document_chunks", sa.Column("chunk_method", sa.String(length=50), nullable=True))
    op.add_column("document_chunks", sa.Column("content_format", sa.String(length=50), nullable=True))
    op.create_foreign_key(
        "fk_document_chunks_parse_run_id_document_parse_runs",
        "document_chunks",
        "document_parse_runs",
        ["parse_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_document_chunks_parse_run_id", "document_chunks", ["parse_run_id"])

    op.create_table(
        "document_chunk_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("block_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("block_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["block_id"], ["document_blocks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chunk_id", "block_order", name="uq_document_chunk_blocks_chunk_order"),
    )
    op.create_index("ix_document_chunk_blocks_block_id", "document_chunk_blocks", ["block_id"])
    op.create_index("ix_document_chunk_blocks_chunk_id", "document_chunk_blocks", ["chunk_id"])


def downgrade() -> None:
    op.drop_index("ix_document_chunk_blocks_chunk_id", table_name="document_chunk_blocks")
    op.drop_index("ix_document_chunk_blocks_block_id", table_name="document_chunk_blocks")
    op.drop_table("document_chunk_blocks")

    op.drop_index("ix_document_chunks_parse_run_id", table_name="document_chunks")
    op.drop_constraint("fk_document_chunks_parse_run_id_document_parse_runs", "document_chunks", type_="foreignkey")
    op.drop_column("document_chunks", "content_format")
    op.drop_column("document_chunks", "chunk_method")
    op.drop_column("document_chunks", "parse_run_id")

    op.drop_index("ix_document_assets_parse_run_id", table_name="document_assets")
    op.drop_index("ix_document_assets_document_id", table_name="document_assets")
    op.drop_index("ix_document_assets_asset_type", table_name="document_assets")
    op.drop_table("document_assets")

    op.drop_index("ix_document_blocks_parse_run_id", table_name="document_blocks")
    op.drop_index("ix_document_blocks_document_id", table_name="document_blocks")
    op.drop_index("ix_document_blocks_block_type", table_name="document_blocks")
    op.drop_table("document_blocks")

    op.drop_index("ix_document_parse_runs_status", table_name="document_parse_runs")
    op.drop_index("ix_document_parse_runs_is_active", table_name="document_parse_runs")
    op.drop_index("ix_document_parse_runs_document_id", table_name="document_parse_runs")
    op.drop_table("document_parse_runs")
