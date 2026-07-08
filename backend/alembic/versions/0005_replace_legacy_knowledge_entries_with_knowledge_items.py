"""replace legacy knowledge entries with knowledge items

Revision ID: 0005_knowledge_items
Revises: 0004_chunk_embedding_vector
Create Date: 2026-07-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005_knowledge_items"
down_revision: str | None = "0004_chunk_embedding_vector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LEGACY_KNOWLEDGE_TABLES = (
    "entry_review_records",
    "entry_versions",
    "knowledge_entries",
)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    existing_tables = set(inspector.get_table_names())

    _ensure_legacy_tables_are_empty(connection, existing_tables)

    if "entry_review_records" in existing_tables:
        op.drop_table("entry_review_records")
    if "entry_versions" in existing_tables:
        op.drop_table("entry_versions")
    if "knowledge_entries" in existing_tables:
        op.drop_table("knowledge_entries")

    op.create_table(
        "knowledge_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("structured_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("entities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("status", sa.String(length=50), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("source_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_filename", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revises_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["revises_item_id"], ["knowledge_items.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_items_status", "knowledge_items", ["status"])
    op.create_index("ix_knowledge_items_item_type", "knowledge_items", ["item_type"])
    op.create_index("ix_knowledge_items_source_document_id", "knowledge_items", ["source_document_id"])
    op.create_index("ix_knowledge_items_content_hash", "knowledge_items", ["content_hash"])
    op.create_index("ix_knowledge_items_created_at", "knowledge_items", ["created_at"])
    op.create_index("ix_knowledge_items_revises_item_id", "knowledge_items", ["revises_item_id"])

    op.create_table(
        "knowledge_item_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["knowledge_item_id"], ["knowledge_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("knowledge_item_id", "chunk_id", name="uq_knowledge_item_chunks_item_chunk"),
    )
    op.create_index(
        "ix_knowledge_item_chunks_knowledge_item_id",
        "knowledge_item_chunks",
        ["knowledge_item_id"],
    )
    op.create_index("ix_knowledge_item_chunks_chunk_id", "knowledge_item_chunks", ["chunk_id"])
    op.create_index("ix_knowledge_item_chunks_document_id", "knowledge_item_chunks", ["document_id"])

    op.create_table(
        "knowledge_item_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_action", sa.String(length=50), nullable=False),
        sa.Column("from_status", sa.String(length=50), nullable=False),
        sa.Column("to_status", sa.String(length=50), nullable=False),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("reviewer", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["knowledge_item_id"], ["knowledge_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_item_reviews_knowledge_item_id",
        "knowledge_item_reviews",
        ["knowledge_item_id"],
    )
    op.create_index("ix_knowledge_item_reviews_created_at", "knowledge_item_reviews", ["created_at"])

    op.create_table(
        "knowledge_item_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["knowledge_item_id"], ["knowledge_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("knowledge_item_id", "version", name="uq_knowledge_item_versions_item_version"),
    )
    op.create_index(
        "ix_knowledge_item_versions_knowledge_item_id",
        "knowledge_item_versions",
        ["knowledge_item_id"],
    )
    op.create_index("ix_knowledge_item_versions_version", "knowledge_item_versions", ["version"])


def downgrade() -> None:
    # Keep downgrade non-destructive for project safety.
    # Recreating the legacy knowledge_entries system must be explicitly reviewed and approved first.
    pass


def _ensure_legacy_tables_are_empty(connection: sa.Connection, existing_tables: set[str]) -> None:
    non_empty_tables: list[str] = []
    for table_name in LEGACY_KNOWLEDGE_TABLES:
        if table_name not in existing_tables:
            continue
        row_count = connection.scalar(sa.text(f'SELECT COUNT(*) FROM "{table_name}"')) or 0
        if row_count > 0:
            non_empty_tables.append(f"{table_name}={row_count}")

    if non_empty_tables:
        raise RuntimeError(
            "Legacy knowledge_entries system contains non-empty tables; "
            "migration stopped to avoid data loss. "
            "Please confirm a handling strategy before rerunning. "
            f"Tables: {', '.join(non_empty_tables)}"
        )
