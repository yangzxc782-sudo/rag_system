from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from pydantic import BaseModel
import pytest

from app.models.knowledge_item import KnowledgeItem
from app.models.knowledge_item_chunk import KnowledgeItemChunk
from app.models.knowledge_item_source import KnowledgeItemSource
from app.core.errors import KNOWLEDGE_ITEM_VALIDATION_FAILED, BusinessError
from app.schemas.knowledge_item import KnowledgeItemCreate, KnowledgeItemData, KnowledgeItemUpdate
from app.services import knowledge_items


DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
CHUNK_ID = UUID("660e8400-e29b-41d4-a716-446655440001")


class FakeScalarResult:
    def first(self) -> None:
        return None


class FakeDb:
    def __init__(self, *, item: KnowledgeItem | None = None, fail_commit: bool = False) -> None:
        self.item = item
        self.fail_commit = fail_commit
        self.added: list[object] = []
        self.rollbacks = 0

    def get(self, model: object, item_id: UUID) -> object | None:
        from app.models.document import Document
        from app.models.document_chunk import DocumentChunk

        if model is KnowledgeItem:
            return self.item if self.item is not None and self.item.id == item_id else None
        if model is Document and item_id == DOCUMENT_ID:
            return SimpleNamespace(id=DOCUMENT_ID, original_filename="casting.md")
        if model is DocumentChunk and item_id == CHUNK_ID:
            return SimpleNamespace(
                id=CHUNK_ID,
                document_id=DOCUMENT_ID,
                chunk_index=1,
                content="source snapshot",
            )
        return None

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid4()

    def commit(self) -> None:
        if self.fail_commit:
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("commit failed")

    def rollback(self) -> None:
        self.rollbacks += 1

    def refresh(self, _value: object) -> None:
        return None

    def scalars(self, _statement: object) -> FakeScalarResult:
        return FakeScalarResult()


def _make_item(status: str = "draft") -> KnowledgeItem:
    item = KnowledgeItem(
        id=uuid4(),
        item_type="process_rule",
        title="Riser rule",
        content="Place risers near hot spots.",
        content_hash="a" * 64,
        status=status,
        source_document_id=DOCUMENT_ID,
        source_filename="casting.md",
        version=1,
    )
    item.chunks = []
    item.sources = [
        KnowledgeItemSource(
            knowledge_item_id=item.id,
            document_id=DOCUMENT_ID,
            source_filename="casting.md",
        )
    ]
    return item


def test_create_dual_writes_source_fact_chunks_and_projection_before_commit() -> None:
    db = FakeDb()
    item = knowledge_items.create_knowledge_item(
        db,
        KnowledgeItemCreate(
            item_type="process_rule",
            title="Riser rule",
            content="Place risers near hot spots.",
            source_document_id=DOCUMENT_ID,
            source_chunk_ids=[CHUNK_ID],
        ),
    )

    sources = [value for value in db.added if isinstance(value, KnowledgeItemSource)]
    assert len(sources) == 1
    assert sources[0].document_id == DOCUMENT_ID
    assert sources[0].source_filename == "casting.md"
    assert item.source_document_id == DOCUMENT_ID
    assert item.source_filename == "casting.md"
    assert item.sources == sources
    assert len(item.chunks) == 1


def test_source_less_manual_create_requires_no_source_relation() -> None:
    db = FakeDb()

    item = knowledge_items.create_knowledge_item(
        db,
        KnowledgeItemCreate(item_type="term_definition", title="Term", content="Meaning"),
    )

    assert item.source_document_id is None
    assert item.sources == []
    assert not any(isinstance(value, KnowledgeItemSource) for value in db.added)


def test_source_filename_update_changes_projection_without_overwriting_relation_snapshot() -> None:
    item = _make_item()
    db = FakeDb(item=item)

    updated = knowledge_items.update_knowledge_item(
        db,
        item.id,
        KnowledgeItemUpdate(source_filename="renamed.md"),
    )

    assert updated.source_filename == "renamed.md"
    assert updated.sources[0].source_filename == "casting.md"
    assert updated.sources[0].document_id == DOCUMENT_ID


def test_null_source_filename_snapshot_is_never_backfilled_by_projection_update() -> None:
    item = _make_item()
    item.source_filename = None
    item.sources[0].source_filename = None
    db = FakeDb(item=item)

    updated = knowledge_items.update_knowledge_item(
        db,
        item.id,
        KnowledgeItemUpdate(source_filename="later-name.md"),
    )

    assert updated.source_filename == "later-name.md"
    assert updated.sources[0].source_filename is None


def test_source_chunk_update_reuses_identity_relation_and_updates_projection() -> None:
    item = _make_item()
    db = FakeDb(item=item)

    updated = knowledge_items.update_knowledge_item(
        db,
        item.id,
        KnowledgeItemUpdate(source_chunk_ids=[CHUNK_ID]),
    )

    assert len(updated.sources) == 1
    assert updated.sources[0].document_id == updated.chunks[0].document_id == DOCUMENT_ID
    assert updated.source_document_id == DOCUMENT_ID


def test_revision_gets_its_own_source_relation_in_same_transaction() -> None:
    original = _make_item("approved")
    original.chunks = [
        KnowledgeItemChunk(
            knowledge_item_id=original.id,
            chunk_id=CHUNK_ID,
            document_id=DOCUMENT_ID,
            chunk_index=1,
            source_text="source snapshot",
        )
    ]
    db = FakeDb(item=original)

    revision = knowledge_items.revise_knowledge_item(db, original.id)

    sources = [value for value in db.added if isinstance(value, KnowledgeItemSource)]
    assert len(sources) == 1
    assert sources[0].knowledge_item_id == revision.id
    assert revision.sources == sources
    assert revision.source_document_id == DOCUMENT_ID


def test_source_relation_rolls_back_with_item_when_commit_fails() -> None:
    db = FakeDb(fail_commit=True)

    with pytest.raises(BusinessError) as exc_info:
        knowledge_items.create_knowledge_item(
            db,
            KnowledgeItemCreate(
                item_type="process_rule",
                title="Riser rule",
                content="Place risers near hot spots.",
                source_document_id=DOCUMENT_ID,
                source_chunk_ids=[CHUNK_ID],
            ),
        )

    assert exc_info.value.code == KNOWLEDGE_ITEM_VALIDATION_FAILED
    assert any(isinstance(value, KnowledgeItemSource) for value in db.added)
    assert db.rollbacks == 1


def test_rest_knowledge_item_contract_has_no_sources_field() -> None:
    for schema in (KnowledgeItemCreate, KnowledgeItemUpdate, KnowledgeItemData):
        assert issubclass(schema, BaseModel)
        assert "sources" not in schema.model_fields
