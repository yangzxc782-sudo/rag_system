from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.knowledge_item import KnowledgeItem
from app.models.knowledge_item_source import KnowledgeItemSource


def ensure_source_relation(
    db: Session,
    item: KnowledgeItem,
    *,
    document_id: UUID | None,
    source_filename: str | None,
) -> KnowledgeItemSource | None:
    """Dual-write the normalized source fact and singular REST projection.

    ``document_id`` is the source identity. ``source_filename`` is the
    immutable provenance snapshot captured when the relation is created.
    """

    sources = list(getattr(item, "sources", []) or [])
    if document_id is None:
        if sources:
            _synchronize_singular_projection(item, sources)
            return projection_source(item)
        item.source_document_id = None
        item.source_filename = source_filename
        item.sources = []
        return None

    relation = next((source for source in sources if source.document_id == document_id), None)
    if relation is None:
        relation = KnowledgeItemSource(
            knowledge_item_id=item.id,
            document_id=document_id,
            source_filename=source_filename,
        )
        db.add(relation)
        sources.append(relation)
        item.sources = sources
        db.flush()
    else:
        # The Phase 7 compatibility projection remains mutable, but it must
        # never rewrite the relation's creation-time provenance snapshot.
        item.source_document_id = document_id
        item.source_filename = source_filename
        return relation

    _synchronize_singular_projection(item, sources)
    return relation


def ordered_sources(item: KnowledgeItem) -> list[KnowledgeItemSource]:
    return sorted(
        list(getattr(item, "sources", []) or []),
        key=lambda source: (str(getattr(source, "created_at", "") or ""), str(source.document_id)),
    )


def projection_source(item: KnowledgeItem) -> KnowledgeItemSource | None:
    sources = ordered_sources(item)
    return sources[0] if sources else None


def _synchronize_singular_projection(
    item: KnowledgeItem,
    sources: list[KnowledgeItemSource],
) -> None:
    """Maintain the Phase 7 singular projection deterministically.

    The projection is never queried to determine the normalized source set.
    """

    if not sources:
        item.source_document_id = None
        item.source_filename = None
        return

    selected = ordered_sources(item)[0]
    item.source_document_id = selected.document_id
    item.source_filename = selected.source_filename
