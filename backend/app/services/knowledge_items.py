from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import (
    KNOWLEDGE_ITEM_DUPLICATE,
    KNOWLEDGE_ITEM_INVALID_STATUS,
    KNOWLEDGE_ITEM_INVALID_TRANSITION,
    KNOWLEDGE_ITEM_NOT_FOUND,
    KNOWLEDGE_ITEM_REVIEW_FAILED,
    KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND,
    KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND,
    KNOWLEDGE_ITEM_VALIDATION_FAILED,
    KNOWLEDGE_ITEM_VERSION_FAILED,
    BusinessError,
)
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_item import KnowledgeItem
from app.models.knowledge_item_chunk import KnowledgeItemChunk
from app.models.knowledge_item_review import KnowledgeItemReview
from app.models.knowledge_item_version import KnowledgeItemVersion
from app.schemas.knowledge_item import (
    CREATABLE_KNOWLEDGE_ITEM_STATUSES,
    EDITABLE_KNOWLEDGE_ITEM_STATUSES,
    KNOWLEDGE_ITEM_STATUSES,
    KNOWLEDGE_ITEM_TYPES,
    KnowledgeItemCreate,
    KnowledgeItemUpdate,
    confidence_to_float,
)
from app.services.knowledge_sources import ensure_source_relation, ordered_sources, projection_source
from app.services.document_operation_guard import DocumentOperationGuard


_WHITESPACE_PATTERN = re.compile(r"\s+")

_STATUS_TRANSITIONS = {
    "submit": {"draft": "pending_review", "rejected": "pending_review"},
    "approve": {"pending_review": "approved"},
    "reject": {"pending_review": "rejected"},
    "deprecate": {"approved": "deprecated"},
}


@dataclass(frozen=True)
class KnowledgeItemListResult:
    items: list[KnowledgeItem]
    total: int
    limit: int
    offset: int


def normalize_hash_text(value: object) -> str:
    text = "" if value is None else str(value)
    return _WHITESPACE_PATTERN.sub(" ", text.strip())


def compute_content_hash(
    item_type: object,
    source_document_id: UUID | str | None,
    title: object,
    content: object,
) -> str:
    normalized_source_document_id = str(source_document_id) if source_document_id else ""
    payload = "\n".join(
        [
            normalize_hash_text(item_type),
            normalize_hash_text(normalized_source_document_id),
            normalize_hash_text(title),
            normalize_hash_text(content),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def list_knowledge_items(
    db: Session,
    *,
    status: str | None = None,
    item_type: str | None = None,
    source_document_id: UUID | None = None,
    source_filename: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> KnowledgeItemListResult:
    _validate_status_filter(status)
    _validate_item_type_filter(item_type)

    filters = []
    if status is not None:
        filters.append(KnowledgeItem.status == status)
    if item_type is not None:
        filters.append(KnowledgeItem.item_type == item_type)
    if source_document_id is not None:
        filters.append(KnowledgeItem.source_document_id == source_document_id)
    if source_filename:
        filters.append(KnowledgeItem.source_filename.ilike(f"%{source_filename.strip()}%"))

    count_statement = select(func.count()).select_from(KnowledgeItem)
    statement = select(KnowledgeItem).order_by(KnowledgeItem.created_at.desc()).limit(limit).offset(offset)
    if filters:
        count_statement = count_statement.where(*filters)
        statement = statement.where(*filters)

    total = db.scalar(count_statement) or 0
    items = list(db.scalars(statement).all())
    return KnowledgeItemListResult(items=items, total=total, limit=limit, offset=offset)


def get_knowledge_item(db: Session, item_id: UUID) -> KnowledgeItem:
    item = db.get(KnowledgeItem, item_id)
    if item is None:
        raise BusinessError(
            KNOWLEDGE_ITEM_NOT_FOUND,
            "Knowledge item was not found.",
            detail={"item_id": str(item_id)},
            status_code=404,
        )
    return item


def create_knowledge_item(db: Session, payload: KnowledgeItemCreate) -> KnowledgeItem:
    _validate_item_type(payload.item_type)
    status = _normalize_create_status(payload.status)
    title = _normalize_required_text(payload.title, field="title")
    content = _normalize_required_text(payload.content, field="content")

    try:
        source_context = _resolve_source_context(
            db,
            source_document_id=payload.source_document_id,
            source_chunk_ids=payload.source_chunk_ids,
            source_filename=payload.source_filename,
        )
        content_hash = compute_content_hash(payload.item_type, source_context.document_id, title, content)
        _ensure_not_duplicate(
            db,
            item_type=payload.item_type,
            source_document_id=source_context.document_id,
            content_hash=content_hash,
        )
        _guard_document_sources(db, {source_context.document_id})

        item = KnowledgeItem(
            item_type=payload.item_type.strip(),
            title=title,
            content=content,
            content_hash=content_hash,
            structured_data=payload.structured_data,
            entities=payload.entities,
            parameters=payload.parameters,
            conditions=payload.conditions,
            confidence=_to_decimal_confidence(payload.confidence),
            status=status,
            source_document_id=source_context.document_id,
            source_filename=source_context.source_filename,
            created_by=payload.created_by,
            version=1,
        )
        db.add(item)
        db.flush()
        item.chunks = _build_item_chunks(item.id, source_context.chunks)
        ensure_source_relation(
            db,
            item,
            document_id=source_context.document_id,
            source_filename=source_context.source_filename,
        )
        create_version_snapshot(db, item, change_reason="created", created_by=payload.created_by)
        db.commit()
        db.refresh(item)
        return item
    except BusinessError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise BusinessError(
            KNOWLEDGE_ITEM_VALIDATION_FAILED,
            "Knowledge item create failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc


def update_knowledge_item(db: Session, item_id: UUID, payload: KnowledgeItemUpdate) -> KnowledgeItem:
    item = get_knowledge_item(db, item_id)
    if item.status not in EDITABLE_KNOWLEDGE_ITEM_STATUSES:
        raise BusinessError(
            KNOWLEDGE_ITEM_INVALID_STATUS,
            "Only draft or rejected knowledge items can be edited.",
            detail={"item_id": str(item_id), "status": item.status},
            status_code=400,
        )

    try:
        updated_fields = payload.model_fields_set
        current_source = projection_source(item)
        # Document locks precede any Knowledge mutation/flush. In particular,
        # ensure_source_relation may flush and acquire FK locks immediately.
        source_context = None
        source_ids = {source.document_id for source in ordered_sources(item)}
        source_ids.add(item.source_document_id)
        if "source_chunk_ids" in updated_fields:
            source_context = _resolve_source_context(db,
                source_document_id=current_source.document_id if current_source is not None else None,
                source_chunk_ids=payload.source_chunk_ids,
                source_filename=payload.source_filename if "source_filename" in updated_fields else item.source_filename)
            source_ids.add(source_context.document_id)
        _guard_document_sources(db, source_ids)
        if "title" in updated_fields:
            item.title = _normalize_required_text(payload.title, field="title")
        if "content" in updated_fields:
            item.content = _normalize_required_text(payload.content, field="content")
        if "structured_data" in updated_fields:
            item.structured_data = payload.structured_data
        if "entities" in updated_fields:
            item.entities = payload.entities
        if "parameters" in updated_fields:
            item.parameters = payload.parameters
        if "conditions" in updated_fields:
            item.conditions = payload.conditions
        if "confidence" in updated_fields:
            item.confidence = _to_decimal_confidence(payload.confidence)
        if "source_filename" in updated_fields:
            item.source_filename = payload.source_filename
        if "source_chunk_ids" in updated_fields:
            item.source_document_id = source_context.document_id
            item.source_filename = source_context.source_filename
            item.chunks = _build_item_chunks(item.id, source_context.chunks)

        if {"source_chunk_ids", "source_filename"} & updated_fields:
            source_identity = source_context.document_id if "source_chunk_ids" in updated_fields else (
                current_source.document_id if current_source is not None else None
            )
            ensure_source_relation(
                db,
                item,
                document_id=source_identity,
                source_filename=item.source_filename,
            )

        item.content_hash = compute_content_hash(item.item_type, item.source_document_id, item.title, item.content)
        _ensure_not_duplicate(
            db,
            item_type=item.item_type,
            source_document_id=item.source_document_id,
            content_hash=item.content_hash,
            exclude_item_id=item.id,
        )
        item.version += 1
        create_version_snapshot(
            db,
            item,
            change_reason=payload.change_reason,
            created_by=payload.updated_by,
        )
        db.commit()
        db.refresh(item)
        return item
    except BusinessError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise BusinessError(
            KNOWLEDGE_ITEM_VALIDATION_FAILED,
            "Knowledge item update failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc


def get_knowledge_item_chunks(db: Session, item_id: UUID) -> list[KnowledgeItemChunk]:
    get_knowledge_item(db, item_id)
    return list(
        db.scalars(
            select(KnowledgeItemChunk)
            .where(KnowledgeItemChunk.knowledge_item_id == item_id)
            .order_by(KnowledgeItemChunk.chunk_index.asc())
        ).all()
    )


def get_knowledge_item_versions(db: Session, item_id: UUID) -> list[KnowledgeItemVersion]:
    get_knowledge_item(db, item_id)
    return list(
        db.scalars(
            select(KnowledgeItemVersion)
            .where(KnowledgeItemVersion.knowledge_item_id == item_id)
            .order_by(KnowledgeItemVersion.version.asc())
        ).all()
    )


def submit_knowledge_item(
    db: Session,
    item_id: UUID,
    *,
    reviewer: str | None = None,
    review_comment: str | None = None,
) -> KnowledgeItem:
    return _transition_knowledge_item(
        db,
        item_id,
        action="submit",
        reviewer=reviewer,
        review_comment=review_comment,
    )


def approve_knowledge_item(
    db: Session,
    item_id: UUID,
    *,
    reviewer: str | None = None,
    review_comment: str | None = None,
) -> KnowledgeItem:
    return _transition_knowledge_item(
        db,
        item_id,
        action="approve",
        reviewer=reviewer,
        review_comment=review_comment,
    )


def reject_knowledge_item(
    db: Session,
    item_id: UUID,
    *,
    reviewer: str | None = None,
    review_comment: str | None = None,
) -> KnowledgeItem:
    return _transition_knowledge_item(
        db,
        item_id,
        action="reject",
        reviewer=reviewer,
        review_comment=review_comment,
    )


def deprecate_knowledge_item(
    db: Session,
    item_id: UUID,
    *,
    reviewer: str | None = None,
    review_comment: str | None = None,
) -> KnowledgeItem:
    return _transition_knowledge_item(
        db,
        item_id,
        action="deprecate",
        reviewer=reviewer,
        review_comment=review_comment,
    )


def list_knowledge_item_reviews(db: Session, item_id: UUID) -> list[KnowledgeItemReview]:
    get_knowledge_item(db, item_id)
    return list(
        db.scalars(
            select(KnowledgeItemReview)
            .where(KnowledgeItemReview.knowledge_item_id == item_id)
            .order_by(KnowledgeItemReview.created_at.asc())
        ).all()
    )


def revise_knowledge_item(
    db: Session,
    item_id: UUID,
    *,
    created_by: str | None = None,
    change_reason: str | None = None,
) -> KnowledgeItem:
    original = get_knowledge_item(db, item_id)
    if original.status not in {"approved", "deprecated"}:
        raise BusinessError(
            KNOWLEDGE_ITEM_INVALID_TRANSITION,
            "Only approved or deprecated knowledge items can be revised.",
            detail={"item_id": str(item_id), "status": original.status},
            status_code=409,
        )

    try:
        original_sources = ordered_sources(original)
        _guard_document_sources(
            db,
            {source.document_id for source in original_sources},
        )
        original_projection = original_sources[0] if original_sources else None
        source_document_id = original_projection.document_id if original_projection is not None else None
        source_filename = (
            original_projection.source_filename if original_projection is not None else original.source_filename
        )
        revision = KnowledgeItem(
            item_type=original.item_type,
            title=original.title,
            content=original.content,
            content_hash=compute_content_hash(
                original.item_type,
                source_document_id,
                original.title,
                original.content,
            ),
            structured_data=original.structured_data,
            entities=original.entities,
            parameters=original.parameters,
            conditions=original.conditions,
            confidence=original.confidence,
            status="draft",
            source_document_id=source_document_id,
            source_filename=source_filename,
            created_by=created_by,
            version=1,
            revises_item_id=original.id,
        )
        db.add(revision)
        db.flush()
        revision.chunks = [
            KnowledgeItemChunk(
                knowledge_item_id=revision.id,
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                chunk_index=chunk.chunk_index,
                source_text=chunk.source_text,
            )
            for chunk in list(getattr(original, "chunks", []) or [])
        ]
        if original_sources:
            for source in original_sources:
                ensure_source_relation(
                    db,
                    revision,
                    document_id=source.document_id,
                    source_filename=source.source_filename,
                )
        else:
            ensure_source_relation(
                db,
                revision,
                document_id=None,
                source_filename=source_filename,
            )
        create_version_snapshot(
            db,
            revision,
            change_reason=change_reason,
            created_by=created_by,
        )
        db.commit()
        db.refresh(revision)
        return revision
    except BusinessError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise BusinessError(
            KNOWLEDGE_ITEM_VERSION_FAILED,
            "Knowledge item revision failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc


def build_knowledge_item_snapshot(item: KnowledgeItem, source_chunk_ids: list[UUID] | None = None) -> dict[str, Any]:
    chunk_ids = source_chunk_ids
    if chunk_ids is None:
        chunk_ids = [chunk.chunk_id for chunk in list(getattr(item, "chunks", []) or [])]

    return {
        "item_type": item.item_type,
        "title": item.title,
        "content": item.content,
        "structured_data": item.structured_data,
        "entities": item.entities,
        "parameters": item.parameters,
        "conditions": item.conditions,
        "status": item.status,
        "confidence": confidence_to_float(item.confidence),
        "source_document_id": str(item.source_document_id) if item.source_document_id else None,
        "source_filename": item.source_filename,
        "source_chunk_ids": [str(chunk_id) for chunk_id in chunk_ids],
    }


def create_version_snapshot(
    db: Session,
    item: KnowledgeItem,
    *,
    change_reason: str | None,
    created_by: str | None,
) -> KnowledgeItemVersion:
    try:
        version = KnowledgeItemVersion(
            knowledge_item_id=item.id,
            version=item.version,
            snapshot=build_knowledge_item_snapshot(item),
            change_reason=change_reason,
            created_by=created_by,
        )
        db.add(version)
        return version
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            KNOWLEDGE_ITEM_VERSION_FAILED,
            "Knowledge item version snapshot failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc


def _transition_knowledge_item(
    db: Session,
    item_id: UUID,
    *,
    action: str,
    reviewer: str | None,
    review_comment: str | None,
) -> KnowledgeItem:
    item = get_knowledge_item(db, item_id)
    _guard_document_sources(
        db,
        {source.document_id for source in ordered_sources(item)},
    )
    from_status = item.status
    to_status = _assert_transition_allowed(from_status, action)

    try:
        item.status = to_status
        _update_review_fields(item, reviewer=reviewer, review_comment=review_comment)
        _create_review_record(
            db,
            item,
            action=action,
            from_status=from_status,
            to_status=to_status,
            reviewer=reviewer,
            review_comment=review_comment,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return item
    except BusinessError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise BusinessError(
            KNOWLEDGE_ITEM_REVIEW_FAILED,
            "Knowledge item review action failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc


def _assert_transition_allowed(from_status: str, action: str) -> str:
    allowed_sources = _STATUS_TRANSITIONS.get(action, {})
    to_status = allowed_sources.get(from_status)
    if to_status is None:
        raise BusinessError(
            KNOWLEDGE_ITEM_INVALID_TRANSITION,
            "Knowledge item status transition is not allowed.",
            detail={"action": action, "from_status": from_status},
            status_code=409,
        )
    return to_status


def _create_review_record(
    db: Session,
    item: KnowledgeItem,
    *,
    action: str,
    from_status: str,
    to_status: str,
    reviewer: str | None,
    review_comment: str | None,
) -> KnowledgeItemReview:
    try:
        record = KnowledgeItemReview(
            knowledge_item_id=item.id,
            review_action=action,
            from_status=from_status,
            to_status=to_status,
            review_comment=review_comment,
            reviewer=reviewer,
        )
        db.add(record)
        return record
    except Exception as exc:
        raise BusinessError(
            KNOWLEDGE_ITEM_REVIEW_FAILED,
            "Knowledge item review record failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc


def _update_review_fields(
    item: KnowledgeItem,
    *,
    reviewer: str | None,
    review_comment: str | None,
) -> None:
    item.reviewed_by = reviewer
    item.review_comment = review_comment
    item.reviewed_at = datetime.now(timezone.utc)


@dataclass(frozen=True)
class SourceContext:
    document_id: UUID | None
    source_filename: str | None
    chunks: list[DocumentChunk]


def _resolve_source_context(
    db: Session,
    *,
    source_document_id: UUID | None,
    source_chunk_ids: list[UUID] | None,
    source_filename: str | None,
) -> SourceContext:
    document = _validate_source_document(db, source_document_id) if source_document_id else None
    chunks = _load_source_chunks(db, source_document_id, source_chunk_ids)
    document_id = source_document_id

    if chunks:
        chunk_document_ids = {chunk.document_id for chunk in chunks}
        if len(chunk_document_ids) > 1:
            raise BusinessError(
                KNOWLEDGE_ITEM_VALIDATION_FAILED,
                "Source chunks for one knowledge item must belong to the same document.",
                status_code=400,
            )
        chunk_document_id = next(iter(chunk_document_ids))
        if source_document_id is not None and chunk_document_id != source_document_id:
            raise BusinessError(
                KNOWLEDGE_ITEM_VALIDATION_FAILED,
                "Source chunk does not belong to source_document_id.",
                detail={"source_document_id": str(source_document_id), "chunk_document_id": str(chunk_document_id)},
                status_code=400,
            )
        document_id = chunk_document_id
        document = _validate_source_document(db, document_id)

    resolved_filename = source_filename
    if resolved_filename is None and document is not None:
        resolved_filename = getattr(document, "original_filename", None)
    return SourceContext(document_id=document_id, source_filename=resolved_filename, chunks=chunks)


def _validate_source_document(db: Session, source_document_id: UUID) -> Document:
    document = db.get(Document, source_document_id)
    if document is None:
        raise BusinessError(
            KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND,
            "Source document was not found.",
            detail={"source_document_id": str(source_document_id)},
            status_code=404,
        )
    return document


def _load_source_chunks(
    db: Session,
    source_document_id: UUID | None,
    source_chunk_ids: list[UUID] | None,
) -> list[DocumentChunk]:
    del source_document_id
    if not source_chunk_ids:
        return []

    chunks: list[DocumentChunk] = []
    for chunk_id in source_chunk_ids:
        chunk = db.get(DocumentChunk, chunk_id)
        if chunk is None:
            raise BusinessError(
                KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND,
                "Source chunk was not found.",
                detail={"chunk_id": str(chunk_id)},
                status_code=404,
            )
        chunks.append(chunk)
    return chunks


def _build_item_chunks(item_id: UUID, chunks: list[DocumentChunk]) -> list[KnowledgeItemChunk]:
    return [
        KnowledgeItemChunk(
            knowledge_item_id=item_id,
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            chunk_index=chunk.chunk_index,
            source_text=chunk.content,
        )
        for chunk in chunks
    ]


def _ensure_not_duplicate(
    db: Session,
    *,
    item_type: str,
    source_document_id: UUID | None,
    content_hash: str,
    exclude_item_id: UUID | None = None,
) -> None:
    statement = (
        select(KnowledgeItem)
        .where(
            KnowledgeItem.item_type == item_type,
            KnowledgeItem.content_hash == content_hash,
            KnowledgeItem.status != "deprecated",
        )
        .limit(1)
    )
    if source_document_id is None:
        statement = statement.where(KnowledgeItem.source_document_id.is_(None))
    else:
        statement = statement.where(KnowledgeItem.source_document_id == source_document_id)
    if exclude_item_id is not None:
        statement = statement.where(KnowledgeItem.id != exclude_item_id)

    duplicate = db.scalars(statement).first()
    if duplicate is not None:
        raise BusinessError(
            KNOWLEDGE_ITEM_DUPLICATE,
            "Knowledge item with the same source, type, title and content already exists.",
            detail={"duplicate_id": str(getattr(duplicate, "id", ""))},
            status_code=409,
        )


def _validate_item_type(item_type: str) -> None:
    if item_type not in KNOWLEDGE_ITEM_TYPES:
        raise BusinessError(
            KNOWLEDGE_ITEM_VALIDATION_FAILED,
            "Knowledge item_type is invalid.",
            detail={"item_type": item_type},
            status_code=400,
        )


def _validate_status_filter(status: str | None) -> None:
    if status is not None and status not in KNOWLEDGE_ITEM_STATUSES:
        raise BusinessError(
            KNOWLEDGE_ITEM_INVALID_STATUS,
            "Knowledge item status is invalid.",
            detail={"status": status},
            status_code=400,
        )


def _validate_item_type_filter(item_type: str | None) -> None:
    if item_type is not None:
        _validate_item_type(item_type)


def _normalize_create_status(status: str | None) -> str:
    normalized_status = (status or "draft").strip()
    if normalized_status not in CREATABLE_KNOWLEDGE_ITEM_STATUSES:
        raise BusinessError(
            KNOWLEDGE_ITEM_INVALID_STATUS,
            "Knowledge item can only be created as draft or pending_review.",
            detail={"status": normalized_status},
            status_code=400,
        )
    return normalized_status


def _normalize_required_text(value: str | None, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise BusinessError(
            KNOWLEDGE_ITEM_VALIDATION_FAILED,
            f"Knowledge item {field} must not be empty.",
            detail={"field": field},
            status_code=400,
        )
    return normalized


def _to_decimal_confidence(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.0001"))


def _guard_document_sources(
    db: Session,
    document_ids: set[UUID | None],
) -> None:
    concrete_ids = {document_id for document_id in document_ids if document_id is not None}
    if concrete_ids:
        DocumentOperationGuard(db).lock_normal_many(concrete_ids)
