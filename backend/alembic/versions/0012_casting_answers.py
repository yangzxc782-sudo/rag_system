"""Allow independently verified engineering answers; preserve all existing rows.

Revision ID: 0012_casting_answers
Revises: 0011_casting_storage
Recovery: disable new casting execution and retain schema/data. No data deletion.
"""
from alembic import op

revision = "0012_casting_answers"
down_revision = "0011_casting_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_qa_turns_outcome", "qa_turns", type_="check")
    op.create_check_constraint("ck_qa_turns_outcome", "qa_turns",
        "outcome IS NULL OR outcome IN ('answer', 'no_context', 'clarification', 'casting_design')")


def downgrade() -> None:
    raise RuntimeError("0012 retains casting answers. Disable new execution and retain schema; manual recovery requires a reviewed backup plan.")
