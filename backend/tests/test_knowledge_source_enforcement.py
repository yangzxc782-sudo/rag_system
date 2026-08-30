from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from app.models.knowledge_item import KnowledgeItem
from app.models.knowledge_item_chunk import KnowledgeItemChunk
from app.models.knowledge_item_source import KnowledgeItemSource
from app.models.knowledge_item_version import KnowledgeItemVersion
from app.services.knowledge_items import build_knowledge_item_snapshot


BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATION_PATH = BACKEND_DIR / "alembic" / "versions" / "0008_phase10_enforce.py"
COMPOSITE_FK_NAME = "fk_knowledge_item_chunks_item_document_source"


class RecordingOp:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def execute(self, statement: object) -> None:
        self.calls.append(("execute", str(statement)))

    def create_foreign_key(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("create_foreign_key", args, kwargs))

    def drop_constraint(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("drop_constraint", args, kwargs))


def load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase10_0008", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_runs_fail_closed_preflight_before_adding_composite_fk() -> None:
    migration = load_migration()
    recorder = RecordingOp()
    migration.op = recorder

    migration.upgrade()

    operation_names = [str(call[0]) for call in recorder.calls]
    assert operation_names[-1] == "create_foreign_key"
    assert operation_names[:-1] == ["execute"]

    preflight_sql = str(recorder.calls[0][1]).lower()
    assert "information_schema.columns" in preflight_sql
    assert "knowledge_item_id" in preflight_sql
    assert "document_id" in preflight_sql
    assert "chunk_id" in preflight_sql
    assert "is_nullable <> 'no'" in preflight_sql
    assert "knowledge_items" in preflight_sql
    assert "document_chunks" in preflight_sql
    assert "knowledge_item_sources" in preflight_sql
    assert "is distinct from" in preflight_sql
    assert "having count(*) > 1" in preflight_sql
    assert "unnest(constraint_row.conkey) with ordinality" in preflight_sql
    assert "raise exception" in preflight_sql
    assert "limit 1" in preflight_sql


def test_upgrade_adds_only_the_restrictive_composite_source_fk() -> None:
    migration = load_migration()
    recorder = RecordingOp()
    migration.op = recorder

    migration.upgrade()

    foreign_key_calls = [call for call in recorder.calls if call[0] == "create_foreign_key"]
    assert foreign_key_calls == [
        (
            "create_foreign_key",
            (
                COMPOSITE_FK_NAME,
                "knowledge_item_chunks",
                "knowledge_item_sources",
                ["knowledge_item_id", "document_id"],
                ["knowledge_item_id", "document_id"],
            ),
            {},
        )
    ]


def test_composite_fk_target_is_unique_and_orm_has_no_delete_cascade() -> None:
    source_table = KnowledgeItemSource.__table__
    chunk_table = KnowledgeItemChunk.__table__

    source_uniques = {
        (constraint.name, tuple(constraint.columns.keys()))
        for constraint in source_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "uq_knowledge_item_sources_item_document",
        ("knowledge_item_id", "document_id"),
    ) in source_uniques

    composite_fks = [
        constraint
        for constraint in chunk_table.constraints
        if isinstance(constraint, ForeignKeyConstraint) and constraint.name == COMPOSITE_FK_NAME
    ]
    assert len(composite_fks) == 1
    assert tuple(composite_fks[0].columns.keys()) == ("knowledge_item_id", "document_id")
    assert tuple(element.target_fullname for element in composite_fks[0].elements) == (
        "knowledge_item_sources.knowledge_item_id",
        "knowledge_item_sources.document_id",
    )
    assert composite_fks[0].ondelete is None

    item_table = KnowledgeItem.__table__
    assert not any(
        constraint.referred_table is source_table
        for constraint in item_table.foreign_key_constraints
    )


def test_version_schema_and_snapshot_writer_still_have_no_integrity_semantics() -> None:
    forbidden_fragments = ("hash", "checksum", "signature", "integrity")
    version_columns = {column.name.lower() for column in KnowledgeItemVersion.__table__.columns}
    assert not any(
        fragment in column_name
        for column_name in version_columns
        for fragment in forbidden_fragments
    )

    item = KnowledgeItem(
        item_type="process_rule",
        title="Riser rule",
        content="Keep feeding paths open.",
        content_hash="a" * 64,
        status="approved",
        version=1,
    )
    item.chunks = []
    snapshot_keys = {key.lower() for key in build_knowledge_item_snapshot(item)}
    assert not any(
        fragment in snapshot_key
        for snapshot_key in snapshot_keys
        for fragment in forbidden_fragments
    )


def test_downgrade_only_drops_the_enforce_fk() -> None:
    migration = load_migration()
    recorder = RecordingOp()
    migration.op = recorder

    migration.downgrade()

    assert recorder.calls == [
        (
            "drop_constraint",
            (COMPOSITE_FK_NAME, "knowledge_item_chunks"),
            {"type_": "foreignkey"},
        )
    ]
