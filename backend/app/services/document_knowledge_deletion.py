from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models.knowledge_item import KnowledgeItem
from app.models.knowledge_item_chunk import KnowledgeItemChunk
from app.models.knowledge_item_review import KnowledgeItemReview
from app.models.knowledge_item_source import KnowledgeItemSource
from app.models.knowledge_item_version import KnowledgeItemVersion
from app.services.knowledge_items import compute_content_hash


MAX_REVISION_CLOSURE_ITEMS = 10_000


class DocumentKnowledgeDeletionInvariantError(RuntimeError):
    """Raised when safe, bounded knowledge cleanup cannot be planned."""


@dataclass(frozen=True)
class KnowledgeDeletionState:
    """Knowledge rows re-read only after the candidate item locks are held."""

    items: tuple[KnowledgeItem, ...]
    sources: tuple[KnowledgeItemSource, ...]
    chunks: tuple[KnowledgeItemChunk, ...]
    versions: tuple[KnowledgeItemVersion, ...]


@dataclass(frozen=True)
class SourceProjection:
    document_id: UUID
    source_filename: str | None


@dataclass(frozen=True)
class KnowledgeCleanupPlan:
    affected_item_ids: tuple[UUID, ...]
    surviving_item_ids: tuple[UUID, ...]
    orphan_item_ids: tuple[UUID, ...]
    source_relation_ids_to_delete: tuple[UUID, ...]
    chunk_relation_ids_to_delete: tuple[UUID, ...]
    orphan_version_item_ids: tuple[UUID, ...]
    projection_updates: Mapping[UUID, SourceProjection]
    redacted_version_snapshots: Mapping[UUID, dict[str, object]]
    revision_item_ids_to_clear: tuple[UUID, ...]


class KnowledgeDeletionRepository(Protocol):
    def find_document_item_ids(self, document_id: UUID) -> tuple[UUID, ...]: ...

    def find_revision_closure(self, seed_ids: tuple[UUID, ...]) -> tuple[UUID, ...]: ...

    def lock_items(self, item_ids: tuple[UUID, ...]) -> None: ...

    def load_state(self, item_ids: tuple[UUID, ...]) -> KnowledgeDeletionState: ...

    def delete_document_relations(self, document_id: UUID, item_ids: tuple[UUID, ...]) -> None: ...

    def apply_plan(self, plan: KnowledgeCleanupPlan, state: KnowledgeDeletionState) -> None: ...


def stable_uuid_order(values: Iterable[UUID]) -> tuple[UUID, ...]:
    return tuple(sorted(set(values), key=str))


def cycle_safe_revision_closure(
    seed_ids: Iterable[UUID],
    load_edges: Callable[[tuple[UUID, ...]], Iterable[tuple[UUID, UUID | None]]],
    *,
    max_items: int = MAX_REVISION_CLOSURE_ITEMS,
) -> tuple[UUID, ...]:
    """Expand both revision directions with a visited set and bounded size."""

    visited = set(seed_ids)
    frontier = set(visited)
    if len(visited) > max_items:
        raise DocumentKnowledgeDeletionInvariantError("Revision closure exceeds the configured safety bound.")

    while frontier:
        ordered_frontier = stable_uuid_order(frontier)
        discovered: set[UUID] = set()
        for item_id, parent_id in load_edges(ordered_frontier):
            discovered.add(item_id)
            if parent_id is not None:
                discovered.add(parent_id)

        frontier = discovered - visited
        visited.update(frontier)
        if len(visited) > max_items:
            raise DocumentKnowledgeDeletionInvariantError("Revision closure exceeds the configured safety bound.")

    return stable_uuid_order(visited)


def build_document_knowledge_cleanup_plan(
    document_id: UUID,
    provenance_state: KnowledgeDeletionState,
    post_delete_state: KnowledgeDeletionState,
) -> KnowledgeCleanupPlan:
    """Plan from saved target provenance and post-delete normalized source facts."""

    item_by_id = {item.id: item for item in post_delete_state.items}
    target_sources = [
        source for source in provenance_state.sources if source.document_id == document_id
    ]
    affected_ids = stable_uuid_order(
        source.knowledge_item_id for source in target_sources if source.knowledge_item_id in item_by_id
    )
    affected_set = set(affected_ids)

    if any(
        source.knowledge_item_id in affected_set and source.document_id == document_id
        for source in post_delete_state.sources
    ):
        raise DocumentKnowledgeDeletionInvariantError(
            "Target knowledge source remains after the post-delete database recount."
        )

    remaining_sources_by_item: dict[UUID, list[KnowledgeItemSource]] = {
        item_id: [] for item_id in affected_ids
    }
    for source in post_delete_state.sources:
        if source.knowledge_item_id in affected_set:
            remaining_sources_by_item[source.knowledge_item_id].append(source)

    orphan_ids = stable_uuid_order(
        item_id for item_id in affected_ids if not remaining_sources_by_item[item_id]
    )
    orphan_set = set(orphan_ids)
    surviving_ids = stable_uuid_order(affected_set - orphan_set)

    projections: dict[UUID, SourceProjection] = {}
    for item_id in surviving_ids:
        selected = min(remaining_sources_by_item[item_id], key=_source_order_key)
        projections[item_id] = SourceProjection(
            document_id=selected.document_id,
            source_filename=selected.source_filename,
        )

    source_ids_to_delete = stable_uuid_order(
        source.id
        for source in (*provenance_state.sources, *post_delete_state.sources)
        if source.knowledge_item_id in orphan_set
        or (source.knowledge_item_id in affected_set and source.document_id == document_id)
    )
    chunk_ids_to_delete = stable_uuid_order(
        chunk.id
        for chunk in (*provenance_state.chunks, *post_delete_state.chunks)
        if chunk.knowledge_item_id in orphan_set
        or (chunk.knowledge_item_id in set(surviving_ids) and chunk.document_id == document_id)
    )

    target_chunk_ids_by_item: dict[UUID, set[UUID]] = {item_id: set() for item_id in surviving_ids}
    for chunk in provenance_state.chunks:
        if chunk.knowledge_item_id in target_chunk_ids_by_item and chunk.document_id == document_id:
            target_chunk_ids_by_item[chunk.knowledge_item_id].add(chunk.chunk_id)

    target_filenames_by_item: dict[UUID, set[str]] = {item_id: set() for item_id in surviving_ids}
    for source in target_sources:
        if source.knowledge_item_id in target_filenames_by_item and source.source_filename is not None:
            target_filenames_by_item[source.knowledge_item_id].add(source.source_filename)

    redacted_versions: dict[UUID, dict[str, object]] = {}
    for version in post_delete_state.versions:
        if version.knowledge_item_id not in projections:
            continue
        redacted = _redact_surviving_snapshot(
            version.snapshot,
            deleted_document_id=document_id,
            deleted_source_filenames=target_filenames_by_item[version.knowledge_item_id],
            deleted_chunk_ids=target_chunk_ids_by_item[version.knowledge_item_id],
            replacement=projections[version.knowledge_item_id],
        )
        if redacted != version.snapshot:
            redacted_versions[version.id] = redacted

    revision_links_to_clear = stable_uuid_order(
        item.id
        for item in post_delete_state.items
        if item.revises_item_id is not None and item.revises_item_id in orphan_set
    )

    return KnowledgeCleanupPlan(
        affected_item_ids=affected_ids,
        surviving_item_ids=surviving_ids,
        orphan_item_ids=orphan_ids,
        source_relation_ids_to_delete=source_ids_to_delete,
        chunk_relation_ids_to_delete=chunk_ids_to_delete,
        orphan_version_item_ids=orphan_ids,
        projection_updates=projections,
        redacted_version_snapshots=redacted_versions,
        revision_item_ids_to_clear=revision_links_to_clear,
    )


class DocumentKnowledgeDeletionRepository:
    def __init__(self, db: Session) -> None:
        self._db = db

    def find_document_item_ids(self, document_id: UUID) -> tuple[UUID, ...]:
        statement = select(KnowledgeItemSource.knowledge_item_id).where(
            KnowledgeItemSource.document_id == document_id
        )
        return stable_uuid_order(self._db.scalars(statement).all())

    def find_revision_closure(self, seed_ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
        return cycle_safe_revision_closure(seed_ids, self._load_revision_edges)

    def _load_revision_edges(
        self,
        frontier: tuple[UUID, ...],
    ) -> Iterable[tuple[UUID, UUID | None]]:
        statement = select(KnowledgeItem.id, KnowledgeItem.revises_item_id).where(
            or_(
                KnowledgeItem.id.in_(frontier),
                KnowledgeItem.revises_item_id.in_(frontier),
            )
        )
        return tuple(self._db.execute(statement).all())

    @staticmethod
    def lock_items_statement(item_ids: Sequence[UUID]):
        return (
            select(KnowledgeItem)
            .where(KnowledgeItem.id.in_(stable_uuid_order(item_ids)))
            .order_by(KnowledgeItem.id)
            .with_for_update()
        )

    def lock_items(self, item_ids: tuple[UUID, ...]) -> None:
        if item_ids:
            self._db.scalars(self.lock_items_statement(item_ids)).all()

    def load_state(self, item_ids: tuple[UUID, ...]) -> KnowledgeDeletionState:
        if not item_ids:
            return KnowledgeDeletionState(items=(), sources=(), chunks=(), versions=())

        items = tuple(
            self._db.scalars(
                select(KnowledgeItem)
                .where(KnowledgeItem.id.in_(item_ids))
                .order_by(KnowledgeItem.id)
            ).all()
        )
        sources = tuple(
            self._db.scalars(
                select(KnowledgeItemSource)
                .where(KnowledgeItemSource.knowledge_item_id.in_(item_ids))
                .order_by(
                    KnowledgeItemSource.knowledge_item_id,
                    KnowledgeItemSource.created_at,
                    KnowledgeItemSource.document_id,
                )
            ).all()
        )
        chunks = tuple(
            self._db.scalars(
                select(KnowledgeItemChunk)
                .where(KnowledgeItemChunk.knowledge_item_id.in_(item_ids))
                .order_by(KnowledgeItemChunk.knowledge_item_id, KnowledgeItemChunk.id)
            ).all()
        )
        versions = tuple(
            self._db.scalars(
                select(KnowledgeItemVersion)
                .where(KnowledgeItemVersion.knowledge_item_id.in_(item_ids))
                .order_by(KnowledgeItemVersion.knowledge_item_id, KnowledgeItemVersion.version)
            ).all()
        )
        return KnowledgeDeletionState(
            items=items,
            sources=sources,
            chunks=chunks,
            versions=versions,
        )

    def delete_document_relations(self, document_id: UUID, item_ids: tuple[UUID, ...]) -> None:
        if not item_ids:
            return
        self._db.execute(
            delete(KnowledgeItemChunk).where(
                KnowledgeItemChunk.knowledge_item_id.in_(item_ids),
                KnowledgeItemChunk.document_id == document_id,
            )
        )
        self._db.execute(
            delete(KnowledgeItemSource).where(
                KnowledgeItemSource.knowledge_item_id.in_(item_ids),
                KnowledgeItemSource.document_id == document_id,
            )
        )
        self._db.flush()

    def apply_plan(self, plan: KnowledgeCleanupPlan, state: KnowledgeDeletionState) -> None:
        item_by_id = {item.id: item for item in state.items}
        for item_id, projection in plan.projection_updates.items():
            item = item_by_id[item_id]
            item.source_document_id = projection.document_id
            item.source_filename = projection.source_filename
            item.content_hash = compute_content_hash(
                item.item_type,
                projection.document_id,
                item.title,
                item.content,
            )

        version_by_id = {version.id: version for version in state.versions}
        for version_id, snapshot in plan.redacted_version_snapshots.items():
            version_by_id[version_id].snapshot = snapshot

        for item_id in plan.revision_item_ids_to_clear:
            item_by_id[item_id].revises_item_id = None

        self._db.flush()

        if plan.orphan_item_ids:
            self._db.execute(
                delete(KnowledgeItemChunk).where(
                    KnowledgeItemChunk.knowledge_item_id.in_(plan.orphan_item_ids)
                )
            )
            self._db.execute(
                delete(KnowledgeItemSource).where(
                    KnowledgeItemSource.knowledge_item_id.in_(plan.orphan_item_ids)
                )
            )
            self._db.execute(
                delete(KnowledgeItemReview).where(
                    KnowledgeItemReview.knowledge_item_id.in_(plan.orphan_item_ids)
                )
            )
            self._db.execute(
                delete(KnowledgeItemVersion).where(
                    KnowledgeItemVersion.knowledge_item_id.in_(plan.orphan_item_ids)
                )
            )
            self._db.flush()
            for item_id in plan.orphan_item_ids:
                self._db.execute(delete(KnowledgeItem).where(KnowledgeItem.id == item_id))

        self._db.flush()


class DocumentKnowledgeDeletionService:
    def __init__(
        self,
        db: Session,
        *,
        repository: KnowledgeDeletionRepository | None = None,
    ) -> None:
        self._repository = repository or DocumentKnowledgeDeletionRepository(db)

    def cleanup(self, document_id: UUID) -> KnowledgeCleanupPlan:
        candidate_ids = self._repository.find_document_item_ids(document_id)
        if not candidate_ids:
            empty_state = KnowledgeDeletionState(items=(), sources=(), chunks=(), versions=())
            return build_document_knowledge_cleanup_plan(
                document_id,
                empty_state,
                empty_state,
            )

        closure_ids = self._repository.find_revision_closure(candidate_ids)
        self._repository.lock_items(closure_ids)
        provenance_state = self._repository.load_state(closure_ids)
        self._repository.delete_document_relations(document_id, closure_ids)
        post_delete_state = self._repository.load_state(closure_ids)
        plan = build_document_knowledge_cleanup_plan(
            document_id,
            provenance_state,
            post_delete_state,
        )
        self._repository.apply_plan(plan, post_delete_state)
        return plan


def _source_order_key(source: KnowledgeItemSource) -> tuple[datetime, str]:
    created_at = source.created_at
    if created_at is None:
        created_at = datetime.min.replace(tzinfo=timezone.utc)
    return created_at, str(source.document_id)


def _redact_surviving_snapshot(
    snapshot: Mapping[str, object],
    *,
    deleted_document_id: UUID,
    deleted_source_filenames: set[str],
    deleted_chunk_ids: set[UUID],
    replacement: SourceProjection,
) -> dict[str, object]:
    redacted = dict(snapshot)
    snapshot_document = redacted.get("source_document_id")
    chunk_ids = redacted.get("source_chunk_ids")
    deleted_chunk_strings = {str(chunk_id) for chunk_id in deleted_chunk_ids}
    source_chunk_strings = (
        {str(chunk_id) for chunk_id in chunk_ids}
        if isinstance(chunk_ids, list)
        else set()
    )

    if snapshot_document is not None:
        if str(snapshot_document) != str(deleted_document_id):
            return redacted
        identifies_deleted_document = True
    elif source_chunk_strings & deleted_chunk_strings:
        identifies_deleted_document = True
    elif redacted.get("source_filename") in deleted_source_filenames:
        raise DocumentKnowledgeDeletionInvariantError(
            "Version provenance is ambiguous without a source document or target chunk identity."
        )
    else:
        identifies_deleted_document = False

    if not identifies_deleted_document:
        return redacted

    redacted["source_document_id"] = str(replacement.document_id)
    redacted["source_filename"] = replacement.source_filename
    if isinstance(chunk_ids, list):
        redacted["source_chunk_ids"] = [
            chunk_id for chunk_id in chunk_ids if str(chunk_id) not in deleted_chunk_strings
        ]
    return redacted
