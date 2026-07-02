"""add document chunk embedding vector

Revision ID: 0004_chunk_embedding_vector
Revises: 0003_chunk_source_meta
Create Date: 2026-07-02
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR

revision: str = "0004_chunk_embedding_vector"
down_revision: str | None = "0003_chunk_source_meta"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column("embedding", VECTOR(1024), nullable=True),
    )
    op.add_column(
        "document_chunks",
        sa.Column("embedding_error_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "document_chunks",
        sa.Column("embedding_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Keep downgrade non-destructive for project safety.
    # Removing embedding vector columns must be explicitly reviewed and approved first.
    pass
