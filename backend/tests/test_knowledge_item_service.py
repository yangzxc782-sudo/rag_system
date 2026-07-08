from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.core.errors import (
    KNOWLEDGE_ITEM_DUPLICATE,
    KNOWLEDGE_ITEM_INVALID_STATUS,
    KNOWLEDGE_ITEM_INVALID_TRANSITION,
    KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND,
    KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND,
    KNOWLEDGE_ITEM_VALIDATION_FAILED,
    BusinessError,
)
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_item import KnowledgeItem
from app.models.knowledge_item_chunk import KnowledgeItemChunk
from app.schemas.knowledge_item import KnowledgeItemCreate, KnowledgeItemUpdate
from app.services import knowledge_items


DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
CHUNK_ID = UUID("660e8400-e29b-41d4-a716-446655440001")


class FakeScalarResult:
    def __init__(self, items: list[object]) -> None:
        self.items = items

    def first(self) -> object | None:
        return self.items[0] if self.items else None

    def all(self) -> list[object]:
        return self.items


class FakeDb:
    def __init__(
        self,
        *,
        documents: dict[UUID, object] | None = None,
        chunks: dict[UUID, object] | None = None,
        items: dict[UUID, KnowledgeItem] | None = None,
        duplicate_items: list[KnowledgeItem] | None = None,
        review_items: list[object] | None = None,
    ) -> None:
        self.documents = documents or {}
        self.chunks = chunks or {}
        self.items = items or {}
        self.duplicate_items = duplicate_items or []
        self.review_items = review_items or []
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.flushed = 0

    def get(self, model: object, item_id: UUID) -> object | None:
        if model is Document:
            return self.documents.get(item_id)
        if model is DocumentChunk:
            return self.chunks.get(item_id)
        if model is KnowledgeItem:
            return self.items.get(item_id)
        return None

    def add(self, item: object) -> None:
        self.added.append(item)

    def add_all(self, items: list[object]) -> None:
        self.added.extend(items)

    def delete(self, item: object) -> None:
        self.deleted.append(item)

    def flush(self) -> None:
        self.flushed += 1
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid4()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def refresh(self, _item: object) -> None:
        return None

    def scalars(self, _statement: object) -> FakeScalarResult:
        if self.review_items:
            return FakeScalarResult(self.review_items)
        return FakeScalarResult(self.duplicate_items)

    def scalar(self, _statement: object) -> int:
        return len(self.duplicate_items)


def fake_document() -> SimpleNamespace:
    return SimpleNamespace(id=DOCUMENT_ID, original_filename="casting.md")


def fake_chunk(chunk_id: UUID = CHUNK_ID, document_id: UUID = DOCUMENT_ID, chunk_index: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        id=chunk_id,
        document_id=document_id,
        chunk_index=chunk_index,
        content="Riser source text snapshot.",
    )


def make_item(status: str = "draft") -> KnowledgeItem:
    content_hash = knowledge_items.compute_content_hash(
        "process_rule",
        DOCUMENT_ID,
        "Riser rule",
        "Place risers near hot spots.",
    )
    item = KnowledgeItem(
        id=uuid4(),
        item_type="process_rule",
        title="Riser rule",
        content="Place risers near hot spots.",
        content_hash=content_hash,
        status=status,
        source_document_id=DOCUMENT_ID,
        source_filename="casting.md",
        version=1,
        confidence=Decimal("0.8000"),
    )
    item.chunks = []
    item.versions = []
    return item


def make_item_chunk(item: KnowledgeItem) -> KnowledgeItemChunk:
    return KnowledgeItemChunk(
        knowledge_item_id=item.id,
        chunk_id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        chunk_index=3,
        source_text="Original source text snapshot.",
    )


def test_compute_content_hash_is_stable_with_empty_source_document_id_and_whitespace() -> None:
    first = knowledge_items.compute_content_hash("process_rule", None, " Riser rule ", "Use risers\n near hot spots.")
    second = knowledge_items.compute_content_hash(" process_rule ", None, "Riser   rule", "Use risers near hot spots.")

    assert first == second
    assert len(first) == 64


def test_create_knowledge_item_defaults_to_draft_and_writes_source_snapshot_and_version() -> None:
    db = FakeDb(documents={DOCUMENT_ID: fake_document()}, chunks={CHUNK_ID: fake_chunk()})
    payload = KnowledgeItemCreate(
        item_type="process_rule",
        title="Riser rule",
        content="Place risers near hot spots.",
        source_document_id=DOCUMENT_ID,
        source_chunk_ids=[CHUNK_ID],
        confidence=0.8,
        created_by="system",
    )

    item = knowledge_items.create_knowledge_item(db, payload)

    assert item.status == "draft"
    assert item.source_filename == "casting.md"
    assert item.confidence == Decimal("0.8000")
    assert len(item.chunks) == 1
    assert item.chunks[0].source_text == "Riser source text snapshot."
    assert item.chunks[0].chunk_index == 3
    versions = [entry for entry in db.added if entry.__class__.__name__ == "KnowledgeItemVersion"]
    assert versions[0].version == 1
    assert versions[0].snapshot["source_chunk_ids"] == [str(CHUNK_ID)]
    assert db.commits == 1


@pytest.mark.parametrize("status", ["approved", "rejected", "deprecated"])
def test_create_knowledge_item_rejects_forbidden_initial_status(status: str) -> None:
    db = FakeDb()
    payload = KnowledgeItemCreate(item_type="process_rule", title="Title", content="Content", status=status)

    with pytest.raises(BusinessError) as exc_info:
        knowledge_items.create_knowledge_item(db, payload)

    assert exc_info.value.code == KNOWLEDGE_ITEM_INVALID_STATUS
    assert db.rollbacks == 0


def test_create_knowledge_item_rejects_duplicate_active_item() -> None:
    duplicate = make_item()
    db = FakeDb(duplicate_items=[duplicate])
    payload = KnowledgeItemCreate(item_type="process_rule", title="Title", content="Content")

    with pytest.raises(BusinessError) as exc_info:
        knowledge_items.create_knowledge_item(db, payload)

    assert exc_info.value.code == KNOWLEDGE_ITEM_DUPLICATE


def test_create_knowledge_item_validates_source_document_and_chunks() -> None:
    payload = KnowledgeItemCreate(
        item_type="process_rule",
        title="Title",
        content="Content",
        source_document_id=DOCUMENT_ID,
    )

    with pytest.raises(BusinessError) as exc_info:
        knowledge_items.create_knowledge_item(FakeDb(), payload)

    assert exc_info.value.code == KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND

    missing_chunk_payload = KnowledgeItemCreate(
        item_type="process_rule",
        title="Title",
        content="Content",
        source_chunk_ids=[CHUNK_ID],
    )
    with pytest.raises(BusinessError) as chunk_exc:
        knowledge_items.create_knowledge_item(FakeDb(), missing_chunk_payload)

    assert chunk_exc.value.code == KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND


def test_create_knowledge_item_rejects_chunk_document_mismatch_and_cross_document_chunks() -> None:
    other_document_id = UUID("770e8400-e29b-41d4-a716-446655440002")
    first_chunk = fake_chunk()
    second_chunk_id = UUID("880e8400-e29b-41d4-a716-446655440003")
    second_chunk = fake_chunk(second_chunk_id, other_document_id, 4)

    mismatch_payload = KnowledgeItemCreate(
        item_type="process_rule",
        title="Title",
        content="Content",
        source_document_id=DOCUMENT_ID,
        source_chunk_ids=[second_chunk_id],
    )
    with pytest.raises(BusinessError) as mismatch_exc:
        knowledge_items.create_knowledge_item(
            FakeDb(documents={DOCUMENT_ID: fake_document()}, chunks={second_chunk_id: second_chunk}),
            mismatch_payload,
        )

    assert mismatch_exc.value.code == KNOWLEDGE_ITEM_VALIDATION_FAILED

    cross_document_payload = KnowledgeItemCreate(
        item_type="process_rule",
        title="Title",
        content="Content",
        source_chunk_ids=[CHUNK_ID, second_chunk_id],
    )
    with pytest.raises(BusinessError) as cross_exc:
        knowledge_items.create_knowledge_item(
            FakeDb(chunks={CHUNK_ID: first_chunk, second_chunk_id: second_chunk}),
            cross_document_payload,
        )

    assert cross_exc.value.code == KNOWLEDGE_ITEM_VALIDATION_FAILED


def test_update_knowledge_item_allows_draft_and_rejected_and_writes_new_version() -> None:
    for editable_status in ("draft", "rejected"):
        item = make_item(editable_status)
        db = FakeDb(items={item.id: item})
        payload = KnowledgeItemUpdate(title=" Updated ", content="Updated content", change_reason="manual edit")

        updated = knowledge_items.update_knowledge_item(db, item.id, payload)

        assert updated.title == "Updated"
        assert updated.content == "Updated content"
        assert updated.version == 2
        versions = [entry for entry in db.added if entry.__class__.__name__ == "KnowledgeItemVersion"]
        assert versions[-1].version == 2
        assert versions[-1].change_reason == "manual edit"
        assert db.commits == 1


@pytest.mark.parametrize("locked_status", ["pending_review", "approved", "deprecated"])
def test_update_knowledge_item_rejects_locked_statuses(locked_status: str) -> None:
    item = make_item(locked_status)
    db = FakeDb(items={item.id: item})

    with pytest.raises(BusinessError) as exc_info:
        knowledge_items.update_knowledge_item(db, item.id, KnowledgeItemUpdate(title="Updated"))

    assert exc_info.value.code == KNOWLEDGE_ITEM_INVALID_STATUS


def test_schema_confidence_from_item_returns_float() -> None:
    from app.schemas.knowledge_item import KnowledgeItemData

    item = make_item()
    item.created_at = datetime(2026, 7, 6, tzinfo=timezone.utc)
    item.updated_at = datetime(2026, 7, 6, tzinfo=timezone.utc)

    data = KnowledgeItemData.from_item(item)

    assert data.confidence == 0.8
    assert isinstance(data.confidence, float)


@pytest.mark.parametrize(
    ("initial_status", "expected_status"),
    [
        ("draft", "pending_review"),
        ("rejected", "pending_review"),
    ],
)
def test_submit_knowledge_item_allows_draft_and_rejected(initial_status: str, expected_status: str) -> None:
    item = make_item(initial_status)
    db = FakeDb(items={item.id: item})

    submitted = knowledge_items.submit_knowledge_item(
        db,
        item.id,
        reviewer="expert",
        review_comment="ready for review",
    )

    assert submitted.status == expected_status
    reviews = [entry for entry in db.added if entry.__class__.__name__ == "KnowledgeItemReview"]
    assert len(reviews) == 1
    assert reviews[0].review_action == "submit"
    assert reviews[0].from_status == initial_status
    assert reviews[0].to_status == expected_status
    assert reviews[0].reviewer == "expert"
    assert db.commits == 1


@pytest.mark.parametrize("initial_status", ["pending_review", "approved", "deprecated"])
def test_submit_knowledge_item_rejects_invalid_statuses(initial_status: str) -> None:
    item = make_item(initial_status)
    db = FakeDb(items={item.id: item})

    with pytest.raises(BusinessError) as exc_info:
        knowledge_items.submit_knowledge_item(db, item.id)

    assert exc_info.value.code == KNOWLEDGE_ITEM_INVALID_TRANSITION


def test_approve_reject_and_deprecate_write_review_records_and_review_fields() -> None:
    approved_item = make_item("pending_review")
    approve_db = FakeDb(items={approved_item.id: approved_item})

    approved = knowledge_items.approve_knowledge_item(
        approve_db,
        approved_item.id,
        reviewer="senior",
        review_comment="approved",
    )

    assert approved.status == "approved"
    assert approved.reviewed_by == "senior"
    assert approved.review_comment == "approved"
    assert approved.reviewed_at is not None
    approve_reviews = [entry for entry in approve_db.added if entry.__class__.__name__ == "KnowledgeItemReview"]
    assert approve_reviews[0].review_action == "approve"
    assert approve_reviews[0].from_status == "pending_review"
    assert approve_reviews[0].to_status == "approved"

    rejected_item = make_item("pending_review")
    reject_db = FakeDb(items={rejected_item.id: rejected_item})
    rejected = knowledge_items.reject_knowledge_item(reject_db, rejected_item.id, reviewer="senior")
    assert rejected.status == "rejected"
    reject_reviews = [entry for entry in reject_db.added if entry.__class__.__name__ == "KnowledgeItemReview"]
    assert reject_reviews[0].review_action == "reject"

    deprecated_item = make_item("approved")
    deprecate_db = FakeDb(items={deprecated_item.id: deprecated_item})
    deprecated = knowledge_items.deprecate_knowledge_item(deprecate_db, deprecated_item.id, reviewer="senior")
    assert deprecated.status == "deprecated"
    assert [
        entry for entry in deprecate_db.added if entry.__class__.__name__ == "KnowledgeItemReview"
    ][0].review_action == "deprecate"


@pytest.mark.parametrize(
    ("action", "initial_status"),
    [
        ("approve_knowledge_item", "draft"),
        ("approve_knowledge_item", "approved"),
        ("reject_knowledge_item", "draft"),
        ("reject_knowledge_item", "rejected"),
        ("deprecate_knowledge_item", "draft"),
        ("deprecate_knowledge_item", "pending_review"),
    ],
)
def test_review_actions_reject_invalid_transitions(action: str, initial_status: str) -> None:
    item = make_item(initial_status)
    db = FakeDb(items={item.id: item})
    service_func = getattr(knowledge_items, action)

    with pytest.raises(BusinessError) as exc_info:
        service_func(db, item.id)

    assert exc_info.value.code == KNOWLEDGE_ITEM_INVALID_TRANSITION


def test_list_knowledge_item_reviews_returns_existing_records() -> None:
    item = make_item("approved")
    review = SimpleNamespace(
        id=uuid4(),
        knowledge_item_id=item.id,
        review_action="approve",
        from_status="pending_review",
        to_status="approved",
        review_comment="ok",
        reviewer="expert",
        created_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
    )
    db = FakeDb(items={item.id: item}, review_items=[review])

    reviews = knowledge_items.list_knowledge_item_reviews(db, item.id)

    assert reviews == [review]


@pytest.mark.parametrize("source_status", ["approved", "deprecated"])
def test_revise_knowledge_item_creates_new_draft_without_modifying_original(source_status: str) -> None:
    original = make_item(source_status)
    original.chunks = [make_item_chunk(original)]
    original_hash = original.content_hash
    db = FakeDb(items={original.id: original})

    revision = knowledge_items.revise_knowledge_item(
        db,
        original.id,
        created_by="editor",
        change_reason="needs update",
    )

    assert original.status == source_status
    assert original.content_hash == original_hash
    assert revision.id != original.id
    assert revision.status == "draft"
    assert revision.revises_item_id == original.id
    assert revision.content_hash == original.content_hash
    assert revision.version == 1
    assert len(revision.chunks) == 1
    assert revision.chunks[0].chunk_id == CHUNK_ID
    assert revision.chunks[0].source_text == "Original source text snapshot."
    versions = [entry for entry in db.added if entry.__class__.__name__ == "KnowledgeItemVersion"]
    assert versions[-1].version == 1
    assert versions[-1].change_reason == "needs update"
    assert db.commits == 1


@pytest.mark.parametrize("source_status", ["draft", "pending_review", "rejected"])
def test_revise_knowledge_item_rejects_untrusted_workflow_statuses(source_status: str) -> None:
    item = make_item(source_status)
    db = FakeDb(items={item.id: item})

    with pytest.raises(BusinessError) as exc_info:
        knowledge_items.revise_knowledge_item(db, item.id)

    assert exc_info.value.code == KNOWLEDGE_ITEM_INVALID_TRANSITION


def test_patch_rules_still_reject_approved_and_deprecated_after_review_api() -> None:
    for locked_status in ("approved", "deprecated"):
        item = make_item(locked_status)
        db = FakeDb(items={item.id: item})

        with pytest.raises(BusinessError) as exc_info:
            knowledge_items.update_knowledge_item(db, item.id, KnowledgeItemUpdate(title="Updated"))

        assert exc_info.value.code == KNOWLEDGE_ITEM_INVALID_STATUS
