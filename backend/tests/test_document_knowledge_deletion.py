from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterable
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from app.models.knowledge_item import KnowledgeItem
from app.models.knowledge_item_chunk import KnowledgeItemChunk
from app.models.knowledge_item_source import KnowledgeItemSource
from app.models.knowledge_item_version import KnowledgeItemVersion
from app.services.document_knowledge_deletion import (
    DocumentKnowledgeDeletionInvariantError,
    DocumentKnowledgeDeletionRepository,
    DocumentKnowledgeDeletionService,
    KnowledgeDeletionState,
    build_document_knowledge_cleanup_plan,
    cycle_safe_revision_closure,
    stable_uuid_order,
)


DOCUMENT_A = UUID("00000000-0000-0000-0000-0000000000a0")
DOCUMENT_B = UUID("00000000-0000-0000-0000-0000000000b0")
DOCUMENT_C = UUID("00000000-0000-0000-0000-0000000000c0")
K1 = UUID("10000000-0000-0000-0000-000000000001")
K2 = UUID("10000000-0000-0000-0000-000000000002")
K3 = UUID("10000000-0000-0000-0000-000000000003")
K4 = UUID("10000000-0000-0000-0000-000000000004")
A1 = UUID("20000000-0000-0000-0000-000000000001")
A2 = UUID("20000000-0000-0000-0000-000000000002")
B1 = UUID("20000000-0000-0000-0000-000000000003")
NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def make_item(
    item_id: UUID,
    *,
    projection_document_id: UUID | None,
    projection_filename: str | None,
    revises_item_id: UUID | None = None,
) -> KnowledgeItem:
    return KnowledgeItem(
        id=item_id,
        item_type="process_rule",
        title=f"Item {item_id}",
        content="Keep this knowledge body unchanged.",
        content_hash="f" * 64,
        status="approved",
        source_document_id=projection_document_id,
        source_filename=projection_filename,
        version=1,
        revises_item_id=revises_item_id,
    )


def make_source(
    relation_id: UUID,
    item_id: UUID,
    document_id: UUID,
    filename: str | None,
    *,
    created_at: datetime,
) -> KnowledgeItemSource:
    return KnowledgeItemSource(
        id=relation_id,
        knowledge_item_id=item_id,
        document_id=document_id,
        source_filename=filename,
        created_at=created_at,
    )


def make_chunk(
    relation_id: UUID,
    item_id: UUID,
    document_id: UUID,
    chunk_id: UUID,
) -> KnowledgeItemChunk:
    return KnowledgeItemChunk(
        id=relation_id,
        knowledge_item_id=item_id,
        document_id=document_id,
        chunk_id=chunk_id,
        chunk_index=0,
        source_text="provenance text",
    )


def make_version(
    version_id: UUID,
    item_id: UUID,
    snapshot: dict[str, object],
) -> KnowledgeItemVersion:
    return KnowledgeItemVersion(
        id=version_id,
        knowledge_item_id=item_id,
        version=1,
        snapshot=snapshot,
    )


def shared_and_orphan_state() -> tuple[KnowledgeDeletionState, dict[str, UUID]]:
    ids = {
        "source_a_k1": UUID("30000000-0000-0000-0000-000000000001"),
        "source_b_k1": UUID("30000000-0000-0000-0000-000000000002"),
        "source_a_k2": UUID("30000000-0000-0000-0000-000000000003"),
        "chunk_a_k1": UUID("40000000-0000-0000-0000-000000000001"),
        "chunk_b_k1": UUID("40000000-0000-0000-0000-000000000002"),
        "chunk_a_k2": UUID("40000000-0000-0000-0000-000000000003"),
        "version_k1_a": UUID("50000000-0000-0000-0000-000000000001"),
        "version_k1_b": UUID("50000000-0000-0000-0000-000000000002"),
        "version_k2": UUID("50000000-0000-0000-0000-000000000003"),
    }
    state = KnowledgeDeletionState(
        items=(
            make_item(
                K1,
                projection_document_id=DOCUMENT_A,
                projection_filename="a.pdf",
                revises_item_id=K2,
            ),
            make_item(
                K2,
                projection_document_id=DOCUMENT_A,
                projection_filename="a.pdf",
            ),
        ),
        sources=(
            make_source(ids["source_a_k1"], K1, DOCUMENT_A, "a.pdf", created_at=NOW),
            make_source(
                ids["source_b_k1"],
                K1,
                DOCUMENT_B,
                "b.pdf",
                created_at=NOW + timedelta(seconds=1),
            ),
            make_source(ids["source_a_k2"], K2, DOCUMENT_A, "a.pdf", created_at=NOW),
        ),
        chunks=(
            make_chunk(ids["chunk_a_k1"], K1, DOCUMENT_A, A1),
            make_chunk(ids["chunk_b_k1"], K1, DOCUMENT_B, B1),
            make_chunk(ids["chunk_a_k2"], K2, DOCUMENT_A, A2),
        ),
        versions=(
            make_version(
                ids["version_k1_a"],
                K1,
                {
                    "content": "Keep this knowledge body unchanged.",
                    "source_document_id": str(DOCUMENT_A),
                    "source_filename": "a.pdf",
                    "source_chunk_ids": [str(A1), str(B1)],
                    "unrelated": {"keep": True},
                },
            ),
            make_version(
                ids["version_k1_b"],
                K1,
                {
                    "content": "Keep this knowledge body unchanged.",
                    "source_document_id": str(DOCUMENT_B),
                    "source_filename": "b.pdf",
                    "source_chunk_ids": [str(B1)],
                },
            ),
            make_version(
                ids["version_k2"],
                K2,
                {
                    "content": "orphan body",
                    "source_document_id": str(DOCUMENT_A),
                    "source_filename": "a.pdf",
                    "source_chunk_ids": [str(A2)],
                },
            ),
        ),
    )
    return state, ids


def state_after_document_relations_deleted(
    state: KnowledgeDeletionState,
    document_id: UUID,
) -> KnowledgeDeletionState:
    return KnowledgeDeletionState(
        items=state.items,
        sources=tuple(source for source in state.sources if source.document_id != document_id),
        chunks=tuple(chunk for chunk in state.chunks if chunk.document_id != document_id),
        versions=state.versions,
    )


def build_plan_after_document_relations_deleted(
    state: KnowledgeDeletionState,
    document_id: UUID = DOCUMENT_A,
):
    return build_document_knowledge_cleanup_plan(
        document_id,
        state,
        state_after_document_relations_deleted(state, document_id),
    )


def test_shared_source_is_preserved_orphan_is_deleted_and_projection_switches() -> None:
    state, ids = shared_and_orphan_state()

    plan = build_plan_after_document_relations_deleted(state)

    assert plan.affected_item_ids == (K1, K2)
    assert plan.surviving_item_ids == (K1,)
    assert plan.orphan_item_ids == (K2,)
    assert plan.source_relation_ids_to_delete == (
        ids["source_a_k1"],
        ids["source_a_k2"],
    )
    assert plan.chunk_relation_ids_to_delete == (
        ids["chunk_a_k1"],
        ids["chunk_a_k2"],
    )
    assert plan.projection_updates[K1].document_id == DOCUMENT_B
    assert plan.projection_updates[K1].source_filename == "b.pdf"
    assert ids["source_b_k1"] not in plan.source_relation_ids_to_delete
    assert ids["chunk_b_k1"] not in plan.chunk_relation_ids_to_delete
    assert plan.orphan_version_item_ids == (K2,)


def test_source_table_not_singular_projection_decides_orphan_status() -> None:
    item = make_item(
        K1,
        projection_document_id=DOCUMENT_B,
        projection_filename="stale-b.pdf",
    )
    state = KnowledgeDeletionState(
        items=(item,),
        sources=(
            make_source(
                UUID("30000000-0000-0000-0000-000000000011"),
                K1,
                DOCUMENT_A,
                "a.pdf",
                created_at=NOW,
            ),
        ),
        chunks=(),
        versions=(),
    )

    plan = build_plan_after_document_relations_deleted(state)

    assert plan.orphan_item_ids == (K1,)
    assert plan.surviving_item_ids == ()
    assert plan.projection_updates == {}


def test_source_less_manual_item_outside_target_relations_is_never_deleted() -> None:
    manual = make_item(K3, projection_document_id=None, projection_filename=None)
    state, _ = shared_and_orphan_state()
    state = KnowledgeDeletionState(
        items=(*state.items, manual),
        sources=state.sources,
        chunks=state.chunks,
        versions=state.versions,
    )

    plan = build_plan_after_document_relations_deleted(state)

    assert K3 not in plan.affected_item_ids
    assert K3 not in plan.orphan_item_ids


def test_projection_selection_is_deterministic_by_created_at_then_document_uuid() -> None:
    item = make_item(K1, projection_document_id=DOCUMENT_A, projection_filename="a.pdf")
    state = KnowledgeDeletionState(
        items=(item,),
        sources=(
            make_source(UUID("30000000-0000-0000-0000-000000000021"), K1, DOCUMENT_A, "a.pdf", created_at=NOW),
            make_source(UUID("30000000-0000-0000-0000-000000000022"), K1, DOCUMENT_C, "c.pdf", created_at=NOW),
            make_source(UUID("30000000-0000-0000-0000-000000000023"), K1, DOCUMENT_B, "b.pdf", created_at=NOW),
        ),
        chunks=(),
        versions=(),
    )

    plan = build_plan_after_document_relations_deleted(state)

    assert plan.projection_updates[K1].document_id == DOCUMENT_B
    assert plan.projection_updates[K1].source_filename == "b.pdf"


def test_surviving_version_redaction_removes_only_deleted_document_provenance() -> None:
    state, ids = shared_and_orphan_state()

    plan = build_plan_after_document_relations_deleted(state)
    redacted = plan.redacted_version_snapshots[ids["version_k1_a"]]

    assert str(DOCUMENT_A) not in str(redacted)
    assert "a.pdf" not in str(redacted)
    assert str(A1) not in str(redacted)
    assert redacted["source_document_id"] == str(DOCUMENT_B)
    assert redacted["source_filename"] == "b.pdf"
    assert redacted["source_chunk_ids"] == [str(B1)]
    assert redacted["content"] == "Keep this knowledge body unchanged."
    assert redacted["unrelated"] == {"keep": True}
    assert ids["version_k1_b"] not in plan.redacted_version_snapshots
    assert ids["version_k2"] not in plan.redacted_version_snapshots


def test_version_redaction_preserves_matching_values_outside_provenance_fields() -> None:
    state, ids = shared_and_orphan_state()
    collision_text = f"Keep casting.pdf, {DOCUMENT_A}, and {A1} in the knowledge body."
    collision_metadata = {
        "filename": "casting.pdf",
        "document": str(DOCUMENT_A),
        "chunk": str(A1),
    }
    state.sources[0].source_filename = "casting.pdf"
    state.versions[0].snapshot = {
        "content": collision_text,
        "source_document_id": str(DOCUMENT_A),
        "source_filename": "casting.pdf",
        "source_chunk_ids": [str(A1), str(B1)],
        "unrelated": collision_metadata,
    }

    plan = build_plan_after_document_relations_deleted(state)
    redacted = plan.redacted_version_snapshots[ids["version_k1_a"]]

    assert redacted["source_document_id"] == str(DOCUMENT_B)
    assert redacted["source_filename"] == "b.pdf"
    assert redacted["source_chunk_ids"] == [str(B1)]
    assert redacted["content"] == collision_text
    assert redacted["unrelated"] == collision_metadata


def test_non_target_document_identity_wins_when_filenames_match() -> None:
    state, ids = shared_and_orphan_state()
    state.sources[0].source_filename = "same-name.pdf"
    state.sources[1].source_filename = "same-name.pdf"
    state.versions[1].snapshot = {
        "content": "B provenance must remain intact.",
        "source_document_id": str(DOCUMENT_B),
        "source_filename": "same-name.pdf",
        "source_chunk_ids": [str(B1)],
    }

    plan = build_plan_after_document_relations_deleted(state)

    assert ids["version_k1_b"] not in plan.redacted_version_snapshots
    assert state.versions[1].snapshot["source_document_id"] == str(DOCUMENT_B)
    assert state.versions[1].snapshot["source_filename"] == "same-name.pdf"


def test_filename_only_provenance_is_ambiguous_and_fails_closed() -> None:
    state, _ = shared_and_orphan_state()
    state.sources[0].source_filename = "same-name.pdf"
    state.versions[0].snapshot = {
        "content": "Do not expose this body in an error.",
        "source_document_id": None,
        "source_filename": "same-name.pdf",
        "source_chunk_ids": [],
    }

    with pytest.raises(DocumentKnowledgeDeletionInvariantError) as exc_info:
        build_plan_after_document_relations_deleted(state)

    error_text = str(exc_info.value)
    assert "Do not expose this body" not in error_text
    assert "same-name.pdf" not in error_text
    assert "source document" in error_text.lower()


def test_missing_document_identity_can_use_target_chunk_identity() -> None:
    state, ids = shared_and_orphan_state()
    state.versions[0].snapshot = {
        "content": "Keep this content.",
        "source_document_id": None,
        "source_filename": "a.pdf",
        "source_chunk_ids": [str(A1), str(B1)],
    }

    plan = build_plan_after_document_relations_deleted(state)
    redacted = plan.redacted_version_snapshots[ids["version_k1_a"]]

    assert redacted["source_document_id"] == str(DOCUMENT_B)
    assert redacted["source_filename"] == "b.pdf"
    assert redacted["source_chunk_ids"] == [str(B1)]


def test_revision_links_are_cleared_without_deleting_a_surviving_revision() -> None:
    state, _ = shared_and_orphan_state()

    plan = build_plan_after_document_relations_deleted(state)

    assert plan.revision_item_ids_to_clear == (K1,)
    assert K1 in plan.surviving_item_ids


def test_orphan_child_does_not_delete_or_rewrite_its_surviving_parent() -> None:
    state, _ = shared_and_orphan_state()
    state.items[0].revises_item_id = None
    state.items[1].revises_item_id = K1

    plan = build_plan_after_document_relations_deleted(state)

    assert plan.revision_item_ids_to_clear == ()
    assert plan.orphan_item_ids == (K2,)
    assert plan.surviving_item_ids == (K1,)


def test_all_orphan_revision_cycle_links_are_cleared_before_item_deletes() -> None:
    item_1 = make_item(
        K1,
        projection_document_id=DOCUMENT_A,
        projection_filename="a.pdf",
        revises_item_id=K2,
    )
    item_2 = make_item(
        K2,
        projection_document_id=DOCUMENT_A,
        projection_filename="a.pdf",
        revises_item_id=K1,
    )
    state = KnowledgeDeletionState(
        items=(item_1, item_2),
        sources=(
            make_source(UUID("30000000-0000-0000-0000-000000000031"), K1, DOCUMENT_A, "a.pdf", created_at=NOW),
            make_source(UUID("30000000-0000-0000-0000-000000000032"), K2, DOCUMENT_A, "a.pdf", created_at=NOW),
        ),
        chunks=(),
        versions=(),
    )

    plan = build_plan_after_document_relations_deleted(state)

    assert plan.orphan_item_ids == (K1, K2)
    assert plan.revision_item_ids_to_clear == (K1, K2)


def test_cycle_safe_revision_closure_covers_ancestors_descendants_and_self_cycle() -> None:
    parent_by_item = {
        K1: K2,
        K2: K3,
        K3: K1,
        K4: K4,
    }

    def load_edges(frontier: tuple[UUID, ...]) -> Iterable[tuple[UUID, UUID | None]]:
        frontier_set = set(frontier)
        return [
            (item_id, parent_id)
            for item_id, parent_id in parent_by_item.items()
            if item_id in frontier_set or parent_id in frontier_set
        ]

    assert cycle_safe_revision_closure((K1, K4), load_edges) == (K1, K2, K3, K4)


def test_overlapping_revision_closure_is_finite_and_deterministic() -> None:
    parent_by_item = {K2: K1, K3: K1, K4: K2}
    calls = 0

    def load_edges(frontier: tuple[UUID, ...]) -> Iterable[tuple[UUID, UUID | None]]:
        nonlocal calls
        calls += 1
        frontier_set = set(frontier)
        return [
            (item_id, parent_id)
            for item_id, parent_id in parent_by_item.items()
            if item_id in frontier_set or parent_id in frontier_set
        ]

    assert cycle_safe_revision_closure((K2, K3), load_edges) == (K1, K2, K3, K4)
    assert calls <= 3


def test_revision_closure_at_safety_ceiling_completes_without_truncation() -> None:
    parent_by_item = {K2: K1, K3: K2}

    def load_edges(frontier: tuple[UUID, ...]) -> Iterable[tuple[UUID, UUID | None]]:
        frontier_set = set(frontier)
        return [
            (item_id, parent_id)
            for item_id, parent_id in parent_by_item.items()
            if item_id in frontier_set or parent_id in frontier_set
        ]

    assert cycle_safe_revision_closure((K1,), load_edges, max_items=3) == (K1, K2, K3)


class CeilingRepository:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def find_document_item_ids(self, _document_id: UUID) -> tuple[UUID, ...]:
        self.calls.append("find_candidates")
        return (K1,)

    def find_revision_closure(self, seed_ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
        self.calls.append("find_closure")
        parent_by_item = {K2: K1, K3: K2}

        def load_edges(frontier: tuple[UUID, ...]) -> Iterable[tuple[UUID, UUID | None]]:
            frontier_set = set(frontier)
            return [
                (item_id, parent_id)
                for item_id, parent_id in parent_by_item.items()
                if item_id in frontier_set or parent_id in frontier_set
            ]

        return cycle_safe_revision_closure(seed_ids, load_edges, max_items=2)

    def lock_items(self, _item_ids: tuple[UUID, ...]) -> None:
        self.calls.append("lock")

    def load_state(self, _item_ids: tuple[UUID, ...]) -> KnowledgeDeletionState:
        self.calls.append("load_state")
        raise AssertionError("state must not be loaded after closure failure")

    def apply_plan(self, _plan: object, _state: KnowledgeDeletionState) -> None:
        self.calls.append("apply")


def test_revision_closure_over_ceiling_fails_before_lock_or_delete() -> None:
    repository = CeilingRepository()
    service = DocumentKnowledgeDeletionService(SimpleNamespace(), repository=repository)

    with pytest.raises(DocumentKnowledgeDeletionInvariantError):
        service.cleanup(DOCUMENT_A)

    assert repository.calls == ["find_candidates", "find_closure"]


def test_stable_uuid_order_and_lock_statement_use_for_update() -> None:
    ordered = stable_uuid_order((K4, K2, K1, K3, K2))
    statement = DocumentKnowledgeDeletionRepository.lock_items_statement(ordered)
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    assert ordered == (K1, K2, K3, K4)
    assert "ORDER BY knowledge_items.id" in sql
    assert sql.rstrip().endswith("FOR UPDATE")


class ContractRepository:
    def __init__(self, *, state: KnowledgeDeletionState) -> None:
        self.state = state
        self.calls: list[str] = []
        self.applied_plan = None
        self.load_count = 0

    def find_document_item_ids(self, _document_id: UUID) -> tuple[UUID, ...]:
        self.calls.append("find_candidates")
        return (K1,)

    def find_revision_closure(self, _seed_ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
        self.calls.append("find_closure")
        return (K1, K2)

    def lock_items(self, _item_ids: tuple[UUID, ...]) -> None:
        self.calls.append("lock")

    def load_state(self, _item_ids: tuple[UUID, ...]) -> KnowledgeDeletionState:
        self.load_count += 1
        self.calls.append(f"load_state_after_lock_{self.load_count}")
        return self.state

    def delete_document_relations(self, _document_id: UUID, _item_ids: tuple[UUID, ...]) -> None:
        self.calls.append("delete_document_relations")

    def apply_plan(self, plan: object, _state: KnowledgeDeletionState) -> None:
        self.calls.append("apply")
        self.applied_plan = plan


class RecountRepository:
    def __init__(
        self,
        *,
        before_delete: KnowledgeDeletionState,
        after_delete: KnowledgeDeletionState,
    ) -> None:
        self.before_delete = before_delete
        self.after_delete = after_delete
        self.calls: list[str] = []
        self.load_count = 0
        self.applied_plan = None

    def find_document_item_ids(self, _document_id: UUID) -> tuple[UUID, ...]:
        self.calls.append("find_candidates")
        return (K1,)

    def find_revision_closure(self, _seed_ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
        self.calls.append("find_closure")
        return (K1,)

    def lock_items(self, _item_ids: tuple[UUID, ...]) -> None:
        self.calls.append("lock")

    def load_state(self, _item_ids: tuple[UUID, ...]) -> KnowledgeDeletionState:
        self.load_count += 1
        self.calls.append(f"load_state_{self.load_count}")
        return self.before_delete if self.load_count == 1 else self.after_delete

    def delete_document_relations(self, _document_id: UUID, _item_ids: tuple[UUID, ...]) -> None:
        self.calls.append("delete_document_relations")

    def apply_plan(self, plan: object, _state: KnowledgeDeletionState) -> None:
        self.calls.append("apply")
        self.applied_plan = plan


def test_service_recounts_post_delete_sources_before_preserving_shared_item() -> None:
    before, _ = shared_and_orphan_state()
    before = KnowledgeDeletionState(
        items=(before.items[0],),
        sources=(before.sources[0], before.sources[1]),
        chunks=(before.chunks[0], before.chunks[1]),
        versions=(before.versions[0], before.versions[1]),
    )
    after = state_after_document_relations_deleted(before, DOCUMENT_A)
    repository = RecountRepository(before_delete=before, after_delete=after)
    service = DocumentKnowledgeDeletionService(SimpleNamespace(), repository=repository)

    plan = service.cleanup(DOCUMENT_A)

    assert repository.calls == [
        "find_candidates",
        "find_closure",
        "lock",
        "load_state_1",
        "delete_document_relations",
        "load_state_2",
        "apply",
    ]
    assert plan.surviving_item_ids == (K1,)
    assert plan.orphan_item_ids == ()
    assert plan.projection_updates[K1].document_id == DOCUMENT_B


def test_service_recounts_empty_post_delete_sources_before_marking_orphan() -> None:
    before, _ = shared_and_orphan_state()
    before = KnowledgeDeletionState(
        items=(before.items[1],),
        sources=(before.sources[2],),
        chunks=(before.chunks[2],),
        versions=(before.versions[2],),
    )
    after = state_after_document_relations_deleted(before, DOCUMENT_A)
    repository = RecountRepository(before_delete=before, after_delete=after)
    service = DocumentKnowledgeDeletionService(SimpleNamespace(), repository=repository)

    plan = service.cleanup(DOCUMENT_A)

    assert repository.calls.count("load_state_2") == 1
    assert repository.calls.index("delete_document_relations") < repository.calls.index("load_state_2")
    assert plan.surviving_item_ids == ()
    assert plan.orphan_item_ids == (K2,)


def test_service_rereads_sources_after_lock_before_deciding_orphan() -> None:
    item = make_item(K1, projection_document_id=DOCUMENT_A, projection_filename="a.pdf")
    repository = ContractRepository(
        state=KnowledgeDeletionState(items=(item,), sources=(), chunks=(), versions=()),
    )
    service = DocumentKnowledgeDeletionService(SimpleNamespace(), repository=repository)

    plan = service.cleanup(DOCUMENT_A)

    assert repository.calls == [
        "find_candidates",
        "find_closure",
        "lock",
        "load_state_after_lock_1",
        "delete_document_relations",
        "load_state_after_lock_2",
        "apply",
    ]
    assert plan.affected_item_ids == ()
    assert plan.orphan_item_ids == ()
    assert repository.applied_plan is plan


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.flushes = 0

    def execute(self, statement: object) -> None:
        self.statements.append(
            str(
                statement.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
        )

    def flush(self) -> None:
        self.flushes += 1


def test_delete_document_relations_deletes_chunks_then_sources_and_flushes() -> None:
    session = RecordingSession()
    repository = DocumentKnowledgeDeletionRepository(session)

    repository.delete_document_relations(DOCUMENT_A, (K1, K2))

    assert len(session.statements) == 2
    assert session.statements[0].startswith("DELETE FROM knowledge_item_chunks")
    assert session.statements[1].startswith("DELETE FROM knowledge_item_sources")
    assert str(DOCUMENT_A) in session.statements[0]
    assert str(DOCUMENT_A) in session.statements[1]
    assert session.flushes == 1


def test_apply_plan_clears_revision_and_deletes_dependents_before_orphan_item() -> None:
    state, _ = shared_and_orphan_state()
    post_delete_state = state_after_document_relations_deleted(state, DOCUMENT_A)
    plan = build_document_knowledge_cleanup_plan(DOCUMENT_A, state, post_delete_state)
    session = RecordingSession()
    repository = DocumentKnowledgeDeletionRepository(session)

    repository.apply_plan(plan, post_delete_state)

    delete_statements = [statement for statement in session.statements if statement.startswith("DELETE")]
    chunk_position = next(index for index, sql in enumerate(delete_statements) if "knowledge_item_chunks" in sql)
    source_position = next(index for index, sql in enumerate(delete_statements) if "knowledge_item_sources" in sql)
    review_position = next(index for index, sql in enumerate(delete_statements) if "knowledge_item_reviews" in sql)
    version_position = next(index for index, sql in enumerate(delete_statements) if "knowledge_item_versions" in sql)
    item_position = next(index for index, sql in enumerate(delete_statements) if "knowledge_items" in sql)

    assert chunk_position < source_position < item_position
    assert review_position < item_position
    assert version_position < item_position
    review_delete = next(sql for sql in delete_statements if "knowledge_item_reviews" in sql)
    version_delete = next(sql for sql in delete_statements if "knowledge_item_versions" in sql)
    assert str(K2) in review_delete and str(K1) not in review_delete
    assert str(K2) in version_delete and str(K1) not in version_delete
    assert state.items[0].revises_item_id is None
    assert state.items[0].source_document_id == DOCUMENT_B
    assert state.items[0].source_filename == "b.pdf"
    assert state.versions[0].snapshot == plan.redacted_version_snapshots[state.versions[0].id]
    assert session.flushes >= 2
