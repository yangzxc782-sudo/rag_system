from __future__ import annotations

from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, inspect

import app.models as models
from app.db.base import Base
from app.models.document import Document
from app.models.knowledge_item import KnowledgeItem


BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATION_PATH = BACKEND_DIR / "alembic" / "versions" / "0007_phase10_expand.py"


def _check_names(table_name: str) -> set[str]:
    table = Base.metadata.tables[table_name]
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def test_phase10_expand_models_are_exported_and_registered() -> None:
    assert hasattr(models, "DocumentDeletionJob")
    assert hasattr(models, "KnowledgeItemSource")
    assert "document_deletion_jobs" in Base.metadata.tables
    assert "knowledge_item_sources" in Base.metadata.tables


def test_documents_have_normal_deletion_status_default_constraint_and_index() -> None:
    table = Base.metadata.tables["documents"]

    assert table.c.deletion_status.nullable is False
    assert table.c.deletion_status.default.arg == "normal"
    assert str(table.c.deletion_status.server_default.arg) == "'normal'"
    assert "ck_documents_deletion_status" in _check_names("documents")
    assert {index.name for index in table.indexes} >= {"ix_documents_deletion_status"}


def test_document_deletion_job_columns_constraints_and_no_document_fk() -> None:
    table = Base.metadata.tables["document_deletion_jobs"]
    assert set(table.c) == {
        table.c.id,
        table.c.document_id,
        table.c.status,
        table.c.current_step,
        table.c.step_attempts,
        table.c.max_attempts,
        table.c.manifest,
        table.c.locked_by,
        table.c.lease_token,
        table.c.locked_at,
        table.c.lease_expires_at,
        table.c.next_retry_at,
        table.c.last_error_code,
        table.c.created_at,
        table.c.updated_at,
    }
    assert not list(table.foreign_key_constraints)
    assert {constraint.name for constraint in table.constraints if isinstance(constraint, UniqueConstraint)} >= {
        "uq_document_deletion_jobs_document_id"
    }
    assert _check_names("document_deletion_jobs") >= {
        "ck_document_deletion_jobs_status",
        "ck_document_deletion_jobs_current_step",
        "ck_document_deletion_jobs_step_attempts",
        "ck_document_deletion_jobs_max_attempts",
        "ck_document_deletion_jobs_lease_fields",
        "ck_document_deletion_jobs_retry_fields",
    }
    assert {index.name for index in table.indexes} >= {"ix_document_deletion_jobs_claimable"}


def test_knowledge_item_sources_identity_unique_and_no_reverse_or_composite_fk() -> None:
    source_table = Base.metadata.tables["knowledge_item_sources"]
    item_table = Base.metadata.tables["knowledge_items"]
    chunk_table = Base.metadata.tables["knowledge_item_chunks"]

    unique_sets = {
        (constraint.name, tuple(constraint.columns.keys()))
        for constraint in source_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("uq_knowledge_item_sources_item_document", ("knowledge_item_id", "document_id")) in unique_sets
    assert source_table.c.source_filename.nullable is True
    assert {index.name for index in source_table.indexes} >= {
        "ix_knowledge_item_sources_knowledge_item_id",
        "ix_knowledge_item_sources_document_id",
    }
    assert not any(
        foreign_key.referred_table is source_table
        for foreign_key in item_table.foreign_key_constraints
    )
    assert not any(
        foreign_key.referred_table is source_table
        for foreign_key in chunk_table.foreign_key_constraints
    )


def test_source_parent_relationships_never_hide_restrict_with_orm_delete_cascade() -> None:
    item_sources = inspect(KnowledgeItem).relationships["sources"]
    document_sources = inspect(Document).relationships["knowledge_item_sources"]

    for relationship in (item_sources, document_sources):
        assert "delete" not in relationship.cascade
        assert "delete-orphan" not in relationship.cascade
        assert relationship.passive_deletes == "all"

    source_table = Base.metadata.tables["knowledge_item_sources"]
    assert {foreign_key.ondelete for foreign_key in source_table.foreign_key_constraints} == {"RESTRICT"}


def test_0007_expand_migration_contract_and_conflict_guard() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision: str = "0007_phase10_expand"' in source
    assert 'down_revision: str | None = "0006_add_document_parse"' in source
    assert "knowledge_items.source_document_id" in source
    assert "knowledge_item_chunks" in source
    assert "conflicting historical source" in source
    assert "affected knowledge item ids" in source
    assert "content_hash" not in source
    assert "knowledge_item_versions" not in source
    assert "fk_knowledge_item_chunks_item_document_source" not in source
    assert "active document deletion jobs" in source
    assert "non-normal document deletion states" in source
    assert "multiple knowledge sources" in source
