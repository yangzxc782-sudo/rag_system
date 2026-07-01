"""add document upload metadata

Revision ID: 0002_add_document_upload_metadata
Revises: 0001_initial_schema
Create Date: 2026-06-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_doc_upload_meta"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "bucket_name",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("'rag-documents'"),
        ),
    )
    op.add_column("documents", sa.Column("mime_type", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("file_hash", sa.String(length=64), nullable=True))
    op.add_column("documents", sa.Column("error_message", sa.Text(), nullable=True))


def downgrade() -> None:
    # Keep downgrade non-destructive for project safety.
    # Removing metadata columns must be explicitly reviewed and approved first.
    pass
