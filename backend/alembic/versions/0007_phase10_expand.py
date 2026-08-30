"""phase 10 expand schema for durable document deletion

Revision ID: 0007_phase10_expand
Revises: 0006_add_document_parse
Create Date: 2026-08-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0007_phase10_expand"
down_revision: str | None = "0006_add_document_parse"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "deletion_status",
            sa.String(length=32),
            server_default=sa.text("'normal'"),
            nullable=True,
        ),
    )
    op.execute("UPDATE documents SET deletion_status = 'normal' WHERE deletion_status IS NULL")
    op.alter_column("documents", "deletion_status", existing_type=sa.String(length=32), nullable=False)
    op.create_check_constraint(
        "ck_documents_deletion_status",
        "documents",
        "deletion_status IN ('normal', 'deleting', 'delete_failed')",
    )
    op.create_index("ix_documents_deletion_status", "documents", ["deletion_status"])

    op.create_table(
        "document_deletion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Immutable recovery identity; deliberately not a ForeignKey to documents.
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("current_step", sa.String(length=64), server_default="delete_opensearch", nullable=False),
        sa.Column("step_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retry_wait', 'delete_failed')",
            name="ck_document_deletion_jobs_status",
        ),
        sa.CheckConstraint(
            "current_step IN ('delete_opensearch', 'delete_minio_derived', "
            "'delete_minio_raw', 'finalize_postgresql')",
            name="ck_document_deletion_jobs_current_step",
        ),
        sa.CheckConstraint("step_attempts >= 0", name="ck_document_deletion_jobs_step_attempts"),
        sa.CheckConstraint("max_attempts > 0", name="ck_document_deletion_jobs_max_attempts"),
        sa.CheckConstraint(
            "(status = 'processing' AND locked_by IS NOT NULL AND lease_token IS NOT NULL "
            "AND locked_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND locked_by IS NULL AND lease_token IS NULL "
            "AND locked_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_document_deletion_jobs_lease_fields",
        ),
        sa.CheckConstraint(
            "(status = 'retry_wait' AND next_retry_at IS NOT NULL) OR "
            "(status <> 'retry_wait' AND next_retry_at IS NULL)",
            name="ck_document_deletion_jobs_retry_fields",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", name="uq_document_deletion_jobs_document_id"),
    )
    op.create_index(
        "ix_document_deletion_jobs_claimable",
        "document_deletion_jobs",
        ["status", "next_retry_at", "lease_expires_at", "created_at"],
    )

    op.create_table(
        "knowledge_item_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        # document_id is the source identity; source_filename is only a provenance snapshot.
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["knowledge_item_id"],
            ["knowledge_items.id"],
            name="fk_knowledge_item_sources_knowledge_item_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_knowledge_item_sources_document_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "knowledge_item_id",
            "document_id",
            name="uq_knowledge_item_sources_item_document",
        ),
    )
    op.create_index(
        "ix_knowledge_item_sources_knowledge_item_id",
        "knowledge_item_sources",
        ["knowledge_item_id"],
    )
    op.create_index(
        "ix_knowledge_item_sources_document_id",
        "knowledge_item_sources",
        ["document_id"],
    )

    # Backfill starts from knowledge_items.source_document_id. Before adding
    # knowledge_item_chunks-derived facts, reject conflicts with the verified
    # historical single-source contract instead of inventing extra sources.
    op.execute(
        """
        DO $$
        DECLARE
            affected_ids text;
        BEGIN
            SELECT string_agg(conflict.id::text, ', ' ORDER BY conflict.id::text)
              INTO affected_ids
              FROM (
                    SELECT DISTINCT ki.id
                      FROM knowledge_items AS ki
                      JOIN knowledge_item_chunks AS kic
                        ON kic.knowledge_item_id = ki.id
                     WHERE ki.source_document_id IS NOT NULL
                       AND kic.document_id <> ki.source_document_id
                     ORDER BY ki.id
                     LIMIT 50
                   ) AS conflict;
            IF affected_ids IS NOT NULL THEN
                RAISE EXCEPTION
                    'conflicting historical source; affected knowledge item ids: %',
                    affected_ids;
            END IF;

            SELECT string_agg(conflict.id::text, ', ' ORDER BY conflict.id::text)
              INTO affected_ids
              FROM (
                    SELECT ki.id
                      FROM knowledge_items AS ki
                      JOIN knowledge_item_chunks AS kic
                        ON kic.knowledge_item_id = ki.id
                     WHERE ki.source_document_id IS NULL
                     GROUP BY ki.id
                    HAVING count(DISTINCT kic.document_id) > 1
                     ORDER BY ki.id
                     LIMIT 50
                   ) AS conflict;
            IF affected_ids IS NOT NULL THEN
                RAISE EXCEPTION
                    'conflicting historical source; affected knowledge item ids: %',
                    affected_ids;
            END IF;
        END
        $$
        """
    )

    op.execute(
        """
        INSERT INTO knowledge_item_sources (
            id, knowledge_item_id, document_id, source_filename, created_at, updated_at
        )
        SELECT md5('knowledge_item_source:' || ki.id::text || ':' || ki.source_document_id::text)::uuid,
               ki.id,
               ki.source_document_id,
               ki.source_filename,
               now(),
               now()
          FROM knowledge_items AS ki
         WHERE ki.source_document_id IS NOT NULL
        ON CONFLICT (knowledge_item_id, document_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO knowledge_item_sources (
            id, knowledge_item_id, document_id, source_filename, created_at, updated_at
        )
        SELECT md5('knowledge_item_source:' || kic.knowledge_item_id::text || ':' || kic.document_id::text)::uuid,
               kic.knowledge_item_id,
               kic.document_id,
               CASE
                   WHEN ki.source_document_id = kic.document_id THEN ki.source_filename
                   ELSE document.original_filename
               END,
               now(),
               now()
          FROM (
                SELECT DISTINCT knowledge_item_id, document_id
                  FROM knowledge_item_chunks
               ) AS kic
          JOIN knowledge_items AS ki ON ki.id = kic.knowledge_item_id
          JOIN documents AS document ON document.id = kic.document_id
        ON CONFLICT (knowledge_item_id, document_id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE knowledge_items AS ki
           SET source_document_id = source.document_id,
               source_filename = source.source_filename
          FROM knowledge_item_sources AS source
         WHERE source.knowledge_item_id = ki.id
           AND ki.source_document_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM document_deletion_jobs) THEN
                RAISE EXCEPTION 'downgrade stopped: active document deletion jobs exist';
            END IF;
            IF EXISTS (SELECT 1 FROM documents WHERE deletion_status <> 'normal') THEN
                RAISE EXCEPTION 'downgrade stopped: non-normal document deletion states exist';
            END IF;
            IF EXISTS (
                SELECT knowledge_item_id
                  FROM knowledge_item_sources
                 GROUP BY knowledge_item_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'downgrade stopped: multiple knowledge sources cannot be represented';
            END IF;
        END
        $$
        """
    )
    op.drop_index("ix_knowledge_item_sources_document_id", table_name="knowledge_item_sources")
    op.drop_index("ix_knowledge_item_sources_knowledge_item_id", table_name="knowledge_item_sources")
    op.drop_table("knowledge_item_sources")
    op.drop_index("ix_document_deletion_jobs_claimable", table_name="document_deletion_jobs")
    op.drop_table("document_deletion_jobs")
    op.drop_index("ix_documents_deletion_status", table_name="documents")
    op.drop_constraint("ck_documents_deletion_status", "documents", type_="check")
    op.drop_column("documents", "deletion_status")

