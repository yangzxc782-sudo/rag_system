from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.models.document import Document
from app.models.knowledge_item import KnowledgeItem
from integration.phase10_fixtures import (
    Phase10KnowledgeFixture,
    assert_postgresql_target_absent,
)
from integration.phase10_support import Phase10TestDocumentFactory


class _Rows:
    def __init__(self, values: list[object] | None = None) -> None:
        self._values = values or []

    def all(self) -> list[object]:
        return list(self._values)


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.get_calls: list[tuple[type[object], UUID]] = []
        self.get_results: dict[tuple[type[object], UUID], object] = {}

    def get(self, model: type[object], identifier: UUID) -> object | None:
        self.get_calls.append((model, identifier))
        return self.get_results.get((model, identifier))

    def scalar(self, statement: object) -> object | None:
        self.statements.append(str(statement))
        return None

    def scalars(self, statement: object) -> _Rows:
        self.statements.append(str(statement))
        return _Rows()


def _knowledge_expectation(target_document_id: UUID) -> Phase10KnowledgeFixture:
    shared_item_id = uuid4()
    orphan_item_id = uuid4()
    shared_source_id = uuid4()
    orphan_source_id = uuid4()
    shared_chunk_id = uuid4()
    orphan_chunk_id = uuid4()
    shared_version_id = uuid4()
    orphan_version_id = uuid4()
    shared_review_id = uuid4()
    orphan_review_id = uuid4()
    return Phase10KnowledgeFixture(
        target_document_id=target_document_id,
        shared_item_id=shared_item_id,
        target_only_item_id=orphan_item_id,
        all_item_ids=(shared_item_id, orphan_item_id),
        target_shared_source_id=shared_source_id,
        target_only_source_id=orphan_source_id,
        source_ids_by_document=(
            (target_document_id, (shared_source_id, orphan_source_id)),
        ),
        chunk_relation_ids=(shared_chunk_id, orphan_chunk_id),
        chunk_relation_ids_by_document=(
            (target_document_id, (shared_chunk_id, orphan_chunk_id)),
        ),
        version_ids=(shared_version_id, orphan_version_id),
        version_ids_by_item=(
            (shared_item_id, shared_version_id),
            (orphan_item_id, orphan_version_id),
        ),
        review_ids=(shared_review_id, orphan_review_id),
        review_ids_by_item=(
            (shared_item_id, shared_review_id),
            (orphan_item_id, orphan_review_id),
        ),
        control_document_id=uuid4(),
    )


def test_core_absence_helper_skips_all_knowledge_specific_assertions() -> None:
    identity = Phase10TestDocumentFactory().create()
    session = _RecordingSession()

    assert_postgresql_target_absent(session, identity, knowledge=None)  # type: ignore[arg-type]

    statements = "\n".join(session.statements)
    assert "document_chunk_blocks" in statements
    assert "knowledge_item_sources" not in statements
    assert "knowledge_item_chunks" not in statements
    assert all(model is not KnowledgeItem for model, _identifier in session.get_calls)


def test_knowledge_aware_absence_helper_preserves_existing_assertions() -> None:
    identity = Phase10TestDocumentFactory().create()
    knowledge = _knowledge_expectation(identity.document_id)
    session = _RecordingSession()

    assert_postgresql_target_absent(session, identity, knowledge=knowledge)  # type: ignore[arg-type]

    statements = "\n".join(session.statements)
    assert "knowledge_item_sources" in statements
    assert "knowledge_item_chunks" in statements
    assert (KnowledgeItem, knowledge.target_only_item_id) in session.get_calls


def test_core_absence_helper_still_fails_when_document_remains() -> None:
    identity = Phase10TestDocumentFactory().create()
    session = _RecordingSession()
    session.get_results[(Document, identity.document_id)] = object()

    with pytest.raises(AssertionError):
        assert_postgresql_target_absent(session, identity, knowledge=None)  # type: ignore[arg-type]
