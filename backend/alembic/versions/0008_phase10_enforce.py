"""enforce normalized knowledge source provenance

Revision ID: 0008_phase10_enforce
Revises: 0007_phase10_expand
Create Date: 2026-08-30
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0008_phase10_enforce"
down_revision: str | None = "0007_phase10_expand"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COMPOSITE_SOURCE_FK = "fk_knowledge_item_chunks_item_document_source"

PREFLIGHT_SQL = r"""
DO $phase10_enforce$
DECLARE
    nullable_or_missing_columns text;
    bad_item_id uuid;
    bad_document_id uuid;
    bad_chunk_id uuid;
BEGIN
    SELECT string_agg(required.column_name, ', ' ORDER BY required.column_name)
      INTO nullable_or_missing_columns
      FROM (
          VALUES ('knowledge_item_id'), ('document_id'), ('chunk_id')
      ) AS required(column_name)
      LEFT JOIN information_schema.columns AS actual
        ON actual.table_schema = current_schema()
       AND actual.table_name = 'knowledge_item_chunks'
       AND actual.column_name = required.column_name
     WHERE actual.column_name IS NULL
        OR actual.is_nullable <> 'NO';

    IF nullable_or_missing_columns IS NOT NULL THEN
        RAISE EXCEPTION
            'Phase 10 enforce preflight failed: knowledge_item_chunks NOT NULL schema drift in columns: %',
            nullable_or_missing_columns;
    END IF;

    SELECT relation.knowledge_item_id, relation.document_id, relation.chunk_id
      INTO bad_item_id, bad_document_id, bad_chunk_id
      FROM knowledge_item_chunks AS relation
      LEFT JOIN knowledge_items AS item
        ON item.id = relation.knowledge_item_id
      LEFT JOIN document_chunks AS chunk
        ON chunk.id = relation.chunk_id
     WHERE relation.knowledge_item_id IS NULL
        OR relation.document_id IS NULL
        OR relation.chunk_id IS NULL
        OR item.id IS NULL
        OR chunk.id IS NULL
        OR chunk.document_id IS DISTINCT FROM relation.document_id
     ORDER BY relation.knowledge_item_id, relation.document_id, relation.chunk_id
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION
            'Phase 10 enforce preflight failed: invalid chunk provenance item=%, document=%, chunk=%',
            bad_item_id, bad_document_id, bad_chunk_id;
    END IF;

    SELECT relation.knowledge_item_id, relation.document_id, relation.chunk_id
      INTO bad_item_id, bad_document_id, bad_chunk_id
      FROM knowledge_item_chunks AS relation
      LEFT JOIN knowledge_item_sources AS source
        ON source.knowledge_item_id = relation.knowledge_item_id
       AND source.document_id = relation.document_id
     WHERE source.knowledge_item_id IS NULL
     ORDER BY relation.knowledge_item_id, relation.document_id, relation.chunk_id
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION
            'Phase 10 enforce preflight failed: missing source relation item=%, document=%, chunk=%',
            bad_item_id, bad_document_id, bad_chunk_id;
    END IF;

    SELECT source.knowledge_item_id, source.document_id
      INTO bad_item_id, bad_document_id
      FROM knowledge_item_sources AS source
     GROUP BY source.knowledge_item_id, source.document_id
    HAVING count(*) > 1
     ORDER BY source.knowledge_item_id, source.document_id
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION
            'Phase 10 enforce preflight failed: duplicate source identity item=%, document=%',
            bad_item_id, bad_document_id;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint AS constraint_row
          JOIN pg_class AS target_table
            ON target_table.oid = constraint_row.conrelid
          JOIN pg_namespace AS target_schema
            ON target_schema.oid = target_table.relnamespace
         WHERE target_table.relname = 'knowledge_item_sources'
           AND target_schema.nspname = current_schema()
           AND constraint_row.conname = 'uq_knowledge_item_sources_item_document'
           AND constraint_row.contype = 'u'
           AND ARRAY(
               SELECT attribute.attname::text
                 FROM unnest(constraint_row.conkey) WITH ORDINALITY AS constrained(attnum, position)
                 JOIN pg_attribute AS attribute
                   ON attribute.attrelid = constraint_row.conrelid
                  AND attribute.attnum = constrained.attnum
                ORDER BY constrained.position
           ) = ARRAY['knowledge_item_id', 'document_id']
    ) THEN
        RAISE EXCEPTION
            'Phase 10 enforce preflight failed: normalized source identity unique constraint is missing';
    END IF;
END
$phase10_enforce$;
"""


def upgrade() -> None:
    op.execute(PREFLIGHT_SQL)
    op.create_foreign_key(
        COMPOSITE_SOURCE_FK,
        "knowledge_item_chunks",
        "knowledge_item_sources",
        ["knowledge_item_id", "document_id"],
        ["knowledge_item_id", "document_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        COMPOSITE_SOURCE_FK,
        "knowledge_item_chunks",
        type_="foreignkey",
    )
