from __future__ import annotations

from pathlib import Path

from sqlalchemy import Boolean, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app import models
from app.db.base import Base


def table(name: str):
    return Base.metadata.tables[name]


def index_columns(table_name: str) -> dict[str, tuple[str, ...]]:
    return {
        index.name: tuple(column.name for column in index.columns)
        for index in table(table_name).indexes
    }


def unique_column_sets(table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table(table_name).constraints
        if isinstance(constraint, UniqueConstraint)
    }


def fk_ondelete(table_name: str, column_name: str) -> set[str | None]:
    return {fk.ondelete for fk in table(table_name).c[column_name].foreign_keys}


def test_document_parse_models_are_exported_and_registered() -> None:
    assert hasattr(models, "DocumentParseRun")
    assert hasattr(models, "DocumentBlock")
    assert hasattr(models, "DocumentAsset")
    assert hasattr(models, "DocumentChunkBlock")

    assert "document_parse_runs" in Base.metadata.tables
    assert "document_blocks" in Base.metadata.tables
    assert "document_assets" in Base.metadata.tables
    assert "document_chunk_blocks" in Base.metadata.tables


def test_document_parse_runs_columns_indexes_and_defaults() -> None:
    parse_runs = table("document_parse_runs")

    expected_columns = {
        "id",
        "document_id",
        "parser_provider",
        "parser_version",
        "parse_mode",
        "status",
        "is_active",
        "input_file_key",
        "output_prefix",
        "output_markdown_key",
        "output_json_key",
        "page_count",
        "block_count",
        "asset_count",
        "error_message",
        "source_metadata",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(parse_runs.c.keys()))
    assert parse_runs.c.document_id.nullable is False
    assert parse_runs.c.parser_provider.nullable is False
    assert parse_runs.c.status.nullable is False
    assert parse_runs.c.is_active.nullable is False
    assert isinstance(parse_runs.c.is_active.type, Boolean)
    assert parse_runs.c.is_active.server_default is not None
    assert parse_runs.c.source_metadata.nullable is True
    assert isinstance(parse_runs.c.source_metadata.type, JSONB)
    assert fk_ondelete("document_parse_runs", "document_id") == {"RESTRICT"}

    indexes = index_columns("document_parse_runs")
    assert indexes["ix_document_parse_runs_document_id"] == ("document_id",)
    assert indexes["ix_document_parse_runs_status"] == ("status",)
    assert indexes["ix_document_parse_runs_is_active"] == ("is_active",)


def test_document_blocks_columns_constraints_and_no_strong_asset_or_parent_fk() -> None:
    blocks = table("document_blocks")

    expected_columns = {
        "id",
        "document_id",
        "parse_run_id",
        "block_index",
        "block_key",
        "block_type",
        "page_start",
        "page_end",
        "bbox",
        "text",
        "markdown",
        "html",
        "latex",
        "caption",
        "parent_block_key",
        "section_path",
        "confidence",
        "source_metadata",
        "created_at",
    }
    assert expected_columns.issubset(set(blocks.c.keys()))
    assert "asset_id" not in blocks.c
    assert blocks.c.block_key.nullable is True
    assert blocks.c.parent_block_key.nullable is True
    assert not blocks.c.parent_block_key.foreign_keys
    assert ("block_key",) not in unique_column_sets("document_blocks")
    assert ("parse_run_id", "block_index") in unique_column_sets("document_blocks")
    assert fk_ondelete("document_blocks", "document_id") == {"RESTRICT"}
    assert fk_ondelete("document_blocks", "parse_run_id") == {"RESTRICT"}

    indexes = index_columns("document_blocks")
    assert indexes["ix_document_blocks_parse_run_id"] == ("parse_run_id",)
    assert indexes["ix_document_blocks_document_id"] == ("document_id",)
    assert indexes["ix_document_blocks_block_type"] == ("block_type",)


def test_document_assets_columns_constraints_and_weak_block_reference() -> None:
    assets = table("document_assets")

    expected_columns = {
        "id",
        "document_id",
        "parse_run_id",
        "asset_type",
        "page_number",
        "asset_key",
        "filename",
        "mime_type",
        "size_bytes",
        "caption",
        "source_block_key",
        "source_metadata",
        "created_at",
    }
    assert expected_columns.issubset(set(assets.c.keys()))
    assert assets.c.asset_key.nullable is False
    assert assets.c.source_block_key.nullable is True
    assert not assets.c.source_block_key.foreign_keys
    assert ("parse_run_id", "asset_key") in unique_column_sets("document_assets")
    assert fk_ondelete("document_assets", "document_id") == {"RESTRICT"}
    assert fk_ondelete("document_assets", "parse_run_id") == {"RESTRICT"}

    indexes = index_columns("document_assets")
    assert indexes["ix_document_assets_parse_run_id"] == ("parse_run_id",)
    assert indexes["ix_document_assets_document_id"] == ("document_id",)
    assert indexes["ix_document_assets_asset_type"] == ("asset_type",)


def test_document_chunk_blocks_constraints_and_non_cascading_foreign_keys() -> None:
    chunk_blocks = table("document_chunk_blocks")

    assert {"id", "chunk_id", "block_id", "block_order", "created_at"}.issubset(set(chunk_blocks.c.keys()))
    assert ("chunk_id", "block_order") in unique_column_sets("document_chunk_blocks")
    assert ("chunk_id", "block_id") not in unique_column_sets("document_chunk_blocks")
    assert fk_ondelete("document_chunk_blocks", "chunk_id") == {"RESTRICT"}
    assert fk_ondelete("document_chunk_blocks", "block_id") == {"RESTRICT"}

    indexes = index_columns("document_chunk_blocks")
    assert indexes["ix_document_chunk_blocks_chunk_id"] == ("chunk_id",)
    assert indexes["ix_document_chunk_blocks_block_id"] == ("block_id",)


def test_document_chunks_extension_preserves_existing_search_and_knowledge_fields() -> None:
    chunks = table("document_chunks")

    assert chunks.c.parse_run_id.nullable is True
    assert chunks.c.chunk_method.nullable is True
    assert chunks.c.content_format.nullable is True
    assert fk_ondelete("document_chunks", "parse_run_id") == {"RESTRICT"}
    assert index_columns("document_chunks")["ix_document_chunks_parse_run_id"] == ("parse_run_id",)

    preserved_columns = {
        "id",
        "document_id",
        "chunk_index",
        "content",
        "token_count",
        "page_start",
        "page_end",
        "section_title",
        "chunk_type",
        "source_metadata",
        "embedding",
        "embedding_model",
        "embedding_dim",
        "embedding_status",
        "embedding_error_message",
        "embedding_updated_at",
    }
    assert preserved_columns.issubset(set(chunks.c.keys()))


def test_migration_only_adds_parse_layer_schema_without_cleanup_logic() -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0006_add_document_parse_intermediate_tables.py"
    )
    migration_text = migration_path.read_text(encoding="utf-8")
    upgrade_text = (
        migration_text
        .split( "def upgrade", maxsplit=1)[1]
        .split( "def downgrade", maxsplit=1)[0]
        .lower()
    )

    assert 'op.create_table(\n        "document_parse_runs"' in migration_text
    assert 'op.create_table(\n        "document_blocks"' in migration_text
    assert 'op.create_table(\n        "document_assets"' in migration_text
    assert 'op.create_table(\n        "document_chunk_blocks"' in migration_text
    assert 'op.add_column("document_chunks"' in migration_text

    forbidden_upgrade_fragments = [
        "drop_table",
        "drop_column",
        "delete(",
        "truncate",
        "execute(",
        "knowledge_items",
        "retrieval_logs",
    ]
    for fragment in forbidden_upgrade_fragments:
        assert fragment not in upgrade_text
