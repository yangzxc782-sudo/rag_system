from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from integration import phase10_fixtures
from app.models.document import Document
from app.models.knowledge_item import KnowledgeItem
from integration.phase10_fixtures import (
    Phase10KnowledgeFixture,
    assert_postgresql_target_absent,
    index_opensearch_fixture,
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


class _BulkClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.body: list[dict[str, object]] | None = None

    def bulk(self, *, body: list[dict[str, object]], refresh: bool) -> dict[str, object]:
        assert refresh is True
        self.body = body
        return self.response


class _RollbackSession:
    def __init__(self) -> None:
        self.rollback_calls = 0

    def rollback(self) -> None:
        self.rollback_calls += 1


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


def test_opensearch_fixture_uses_deterministic_non_zero_1024_dimension_vectors() -> None:
    identity = Phase10TestDocumentFactory().create()
    client = _BulkClient({"errors": False})

    index_opensearch_fixture(client, identity, "phase10-test-v1")

    assert client.body is not None
    sources = client.body[1::2]
    assert len(sources) == len(identity.chunk_ids)
    for source in sources:
        vector = source["embedding"]
        assert isinstance(vector, list)
        assert len(vector) == 1024
        assert any(value != 0.0 for value in vector)
        assert sum(value * value for value in vector) == 1.0


def test_opensearch_fixture_bulk_failure_reports_safe_item_detail() -> None:
    identity = Phase10TestDocumentFactory().create()
    client = _BulkClient(
        {
            "errors": True,
            "items": [
                {
                    "index": {
                        "_id": str(identity.chunk_ids[0]),
                        "status": 400,
                        "error": {
                            "type": "mapper_parsing_exception",
                            "reason": "controlled vector rejection",
                        },
                    }
                }
            ],
        }
    )

    with pytest.raises(AssertionError) as captured:
        index_opensearch_fixture(client, identity, "phase10-test-v1")

    message = str(captured.value)
    assert "index status=400" in message
    assert "type=mapper_parsing_exception" in message
    assert "reason=controlled vector rejection" in message
    assert str(identity.chunk_ids[0]) in message
    assert "embedding" not in message
    assert "_source" not in message


def test_contender_drain_skips_target_drains_unrelated_and_rolls_back() -> None:
    target_job_id = uuid4()
    other_job_ids = (uuid4(), uuid4())
    claims = iter(
        [
            SimpleNamespace(job_id=other_job_ids[0]),
            SimpleNamespace(job_id=other_job_ids[1]),
            None,
        ]
    )
    session = _RollbackSession()
    drain = getattr(
        phase10_fixtures,
        "drain_claimable_jobs_excluding_target",
        None,
    )
    assert callable(drain)

    claimed_ids = drain(
        session,
        target_job_id=target_job_id,
        max_claims=4,
        claim_function=lambda *_args, **_kwargs: next(claims),
    )

    assert claimed_ids == other_job_ids
    assert target_job_id not in claimed_ids
    assert session.rollback_calls == 1


def test_contender_drain_fails_and_rolls_back_if_target_is_returned() -> None:
    target_job_id = uuid4()
    session = _RollbackSession()
    drain = getattr(
        phase10_fixtures,
        "drain_claimable_jobs_excluding_target",
        None,
    )
    assert callable(drain)

    with pytest.raises(AssertionError, match="heartbeat target"):
        drain(
            session,
            target_job_id=target_job_id,
            max_claims=2,
            claim_function=lambda *_args, **_kwargs: SimpleNamespace(
                job_id=target_job_id
            ),
        )

    assert session.rollback_calls == 1


def test_contender_drain_has_a_finite_safety_ceiling_and_rolls_back() -> None:
    session = _RollbackSession()
    drain = getattr(
        phase10_fixtures,
        "drain_claimable_jobs_excluding_target",
        None,
    )
    assert callable(drain)

    with pytest.raises(AssertionError, match="safety limit"):
        drain(
            session,
            target_job_id=uuid4(),
            max_claims=2,
            claim_function=lambda *_args, **_kwargs: SimpleNamespace(job_id=uuid4()),
        )

    assert session.rollback_calls == 1
