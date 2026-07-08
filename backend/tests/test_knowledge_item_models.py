from __future__ import annotations

from pathlib import Path

from sqlalchemy import Numeric, UniqueConstraint

import app.models as models
from app.db.base import Base


BACKEND_DIR = Path(__file__).resolve().parents[1]
MODELS_DIR = BACKEND_DIR / "app" / "models"
MIGRATION_PATH = (
    BACKEND_DIR
    / "alembic"
    / "versions"
    / "0005_replace_legacy_knowledge_entries_with_knowledge_items.py"
)


def test_new_knowledge_item_models_are_exported_and_legacy_models_are_removed() -> None:
    assert hasattr(models, "KnowledgeItem")
    assert hasattr(models, "KnowledgeItemChunk")
    assert hasattr(models, "KnowledgeItemReview")
    assert hasattr(models, "KnowledgeItemVersion")

    assert not hasattr(models, "KnowledgeEntry")
    assert not hasattr(models, "EntryVersion")
    assert not hasattr(models, "EntryReviewRecord")

    assert not (MODELS_DIR / "knowledge_entry.py").exists()
    assert not (MODELS_DIR / "entry_version.py").exists()
    assert not (MODELS_DIR / "entry_review_record.py").exists()


def test_knowledge_item_tables_are_registered_in_metadata() -> None:
    table_names = set(Base.metadata.tables)

    assert "knowledge_items" in table_names
    assert "knowledge_item_chunks" in table_names
    assert "knowledge_item_reviews" in table_names
    assert "knowledge_item_versions" in table_names
    assert "knowledge_entries" not in table_names
    assert "entry_versions" not in table_names
    assert "entry_review_records" not in table_names


def test_knowledge_items_columns_indexes_and_hash_constraints() -> None:
    table = Base.metadata.tables["knowledge_items"]
    required_columns = {
        "id",
        "item_type",
        "title",
        "content",
        "content_hash",
        "structured_data",
        "entities",
        "parameters",
        "conditions",
        "confidence",
        "status",
        "source_document_id",
        "source_filename",
        "created_by",
        "reviewed_by",
        "review_comment",
        "version",
        "reviewed_at",
        "created_at",
        "updated_at",
        "revises_item_id",
    }

    assert required_columns.issubset(set(table.c.keys()))
    assert isinstance(table.c.confidence.type, Numeric)

    index_columns = {tuple(index.columns.keys()): index.unique for index in table.indexes}
    assert ("content_hash",) in index_columns
    assert index_columns[("content_hash",)] is False

    unique_column_sets = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("content_hash",) not in unique_column_sets
    assert ("source_document_id", "item_type", "content_hash") not in unique_column_sets


def test_knowledge_item_chunk_and_version_constraints() -> None:
    chunks_table = Base.metadata.tables["knowledge_item_chunks"]
    versions_table = Base.metadata.tables["knowledge_item_versions"]

    assert "source_text" in chunks_table.c
    assert "snapshot" in versions_table.c

    chunk_unique_sets = {
        tuple(constraint.columns.keys())
        for constraint in chunks_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    version_unique_sets = {
        tuple(constraint.columns.keys())
        for constraint in versions_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("knowledge_item_id", "chunk_id") in chunk_unique_sets
    assert ("knowledge_item_id", "version") in version_unique_sets


def test_document_models_no_longer_reference_legacy_knowledge_entry() -> None:
    assert "KnowledgeEntry" not in (MODELS_DIR / "document.py").read_text(encoding="utf-8")
    assert "KnowledgeEntry" not in (MODELS_DIR / "document_chunk.py").read_text(encoding="utf-8")


def test_migration_replaces_legacy_knowledge_tables_with_protection() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "sa.inspect" in source
    assert "LEGACY_KNOWLEDGE_TABLES" in source
    assert "entry_review_records" in source
    assert "entry_versions" in source
    assert "knowledge_entries" in source
    assert "non-empty" in source
    assert "migration stopped" in source

    review_drop = source.index('op.drop_table("entry_review_records")')
    version_drop = source.index('op.drop_table("entry_versions")')
    entry_drop = source.index('op.drop_table("knowledge_entries")')
    assert review_drop < version_drop < entry_drop

    assert "op.create_table(" in source
    for table_name in [
        "knowledge_items",
        "knowledge_item_chunks",
        "knowledge_item_reviews",
        "knowledge_item_versions",
    ]:
        assert table_name in source
    assert "uq_knowledge_items_content_hash" not in source
    assert "uq_knowledge_items_source_document_item_hash" not in source
