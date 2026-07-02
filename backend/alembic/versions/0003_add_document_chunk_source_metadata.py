"""add document chunk source metadata

Revision ID: 0003_add_document_chunk_source_metadata
Revises: 0002_doc_upload_meta
Create Date: 2026-07-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003_chunk_source_meta"
down_revision: str | None = "0002_doc_upload_meta"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column(
            "source_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # Keep downgrade non-destructive for project safety.
    # Removing source metadata must be explicitly reviewed and approved first.
    pass
