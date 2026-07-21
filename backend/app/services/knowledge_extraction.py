from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    KNOWLEDGE_ITEM_CONFIG_INVALID,
    KNOWLEDGE_ITEM_DUPLICATE,
    KNOWLEDGE_ITEM_EXTRACTION_FAILED,
    KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND,
    KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND,
    BusinessError,
)
from app.extraction.knowledge_parser import parse_extraction_json
from app.extraction.knowledge_prompt import build_knowledge_extraction_prompt
from app.llm.provider import LLMGenerateRequest, LLMGenerateResult, LLMProvider, get_llm_provider
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_item import KnowledgeItem
from app.schemas.knowledge_item import (
    KNOWLEDGE_ITEM_TYPES,
    ExtractedKnowledgeItem,
    KnowledgeExtractionRequest,
    KnowledgeExtractionSkippedDuplicate,
    KnowledgeItemCreate,
)
from app.services import knowledge_items as knowledge_item_service


@dataclass(frozen=True)
class KnowledgeExtractionResult:
    items: list[KnowledgeItem]
    created: int
    skipped_duplicates: list[KnowledgeExtractionSkippedDuplicate]
    status: str
    auto_submit: bool
    llm_provider: str | None
    llm_model: str | None


def select_chunks_for_extraction(
    db: Session,
    request: KnowledgeExtractionRequest,
    settings: Any,
) -> list[DocumentChunk]:
    max_chunks = _normalize_max_chunks(request.max_chunks, settings)
    max_chars = _normalize_max_chars(settings)
    mode = _normalize_mode(request.mode)

    if mode == "document":
        chunks = _select_document_chunks(db, request.document_id, max_chunks)
    else:
        chunks = _select_explicit_chunks(db, request.chunk_ids, request.document_id, max_chunks)

    if not chunks:
        raise BusinessError(
            KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND,
            "No source chunks were found for knowledge extraction.",
            status_code=404,
        )

    total_chars = sum(len(str(getattr(chunk, "content", "") or "")) for chunk in chunks)
    if total_chars > max_chars:
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "Knowledge extraction input exceeds max character limit.",
            detail={"total_chars": total_chars, "max_chars": max_chars},
            status_code=400,
        )
    return chunks


def build_extraction_messages(
    chunks: list[DocumentChunk],
    item_types: list[str] | None,
) -> tuple[str, str]:
    return build_knowledge_extraction_prompt(chunks, item_types=item_types)


def extract_knowledge_items(
    db: Session,
    request: KnowledgeExtractionRequest,
    *,
    settings: Any | None = None,
    llm_provider: LLMProvider | None = None,
) -> KnowledgeExtractionResult:
    settings = settings or get_settings()
    _validate_extraction_settings(settings)
    item_types = _normalize_item_types(request.item_types)
    chunks = select_chunks_for_extraction(db, request, settings)
    system_prompt, user_prompt = build_extraction_messages(chunks, item_types)
    llm_result = _generate_extraction_json(system_prompt, user_prompt, settings, llm_provider)
    candidates = parse_extraction_json(
        llm_result.text,
        allowed_chunk_ids={chunk.id for chunk in chunks},
        allowed_item_types=set(item_types) if item_types is not None else None,
    )
    target_status = "pending_review" if request.auto_submit else "draft"

    created_items: list[KnowledgeItem] = []
    skipped_duplicates: list[KnowledgeExtractionSkippedDuplicate] = []
    for candidate in candidates:
        try:
            item = _create_item_from_candidate(db, candidate, request, chunks)
            if request.auto_submit:
                item = knowledge_item_service.submit_knowledge_item(
                    db,
                    item.id,
                    reviewer=request.created_by,
                    review_comment="auto_submit from knowledge extraction",
                )
            created_items.append(item)
        except BusinessError as error:
            if error.code != KNOWLEDGE_ITEM_DUPLICATE:
                raise
            skipped_duplicates.append(_build_duplicate_skip(candidate, chunks))

    return KnowledgeExtractionResult(
        items=created_items,
        created=len(created_items),
        skipped_duplicates=skipped_duplicates,
        status=target_status,
        auto_submit=request.auto_submit,
        llm_provider=llm_result.provider,
        llm_model=llm_result.model,
    )


def _select_document_chunks(
    db: Session,
    document_id: UUID | None,
    max_chunks: int,
) -> list[DocumentChunk]:
    if document_id is None:
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "document_id is required when extraction mode is document.",
            status_code=400,
        )
    document = db.get(Document, document_id)
    if document is None:
        raise BusinessError(
            KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND,
            "Source document was not found.",
            detail={"document_id": str(document_id)},
            status_code=404,
        )
    chunks = list(
        db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
            .limit(max_chunks)
        ).all()
    )
    return chunks[:max_chunks]


def _select_explicit_chunks(
    db: Session,
    chunk_ids: list[UUID] | None,
    document_id: UUID | None,
    max_chunks: int,
) -> list[DocumentChunk]:
    if not chunk_ids:
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "chunk_ids are required when extraction mode is chunks.",
            status_code=400,
        )
    if len(chunk_ids) > max_chunks:
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "Knowledge extraction chunk count exceeds max_chunks.",
            detail={"chunk_count": len(chunk_ids), "max_chunks": max_chunks},
            status_code=400,
        )

    chunks: list[DocumentChunk] = []
    for chunk_id in chunk_ids:
        chunk = db.get(DocumentChunk, chunk_id)
        if chunk is None:
            raise BusinessError(
                KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND,
                "Source chunk was not found.",
                detail={"chunk_id": str(chunk_id)},
                status_code=404,
            )
        chunks.append(chunk)

    document_ids = {chunk.document_id for chunk in chunks}
    if len(document_ids) > 1:
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "Knowledge extraction chunks must belong to one document in phase 7.",
            status_code=400,
        )
    if document_id is not None and next(iter(document_ids)) != document_id:
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "Source chunks do not belong to document_id.",
            detail={"document_id": str(document_id)},
            status_code=400,
        )
    return chunks


def _generate_extraction_json(
    system_prompt: str,
    user_prompt: str,
    settings: Any,
    llm_provider: LLMProvider | None,
) -> LLMGenerateResult:
    provider = llm_provider or get_llm_provider()
    try:
        return provider.generate(
            LLMGenerateRequest.from_prompt(
                user_prompt,
                system_prompt,
                temperature=0.0,
                max_tokens=getattr(settings, "llm_max_tokens", None),
                json_mode=True,
                think=False,
                think_required=False,
            )
        )
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            KNOWLEDGE_ITEM_EXTRACTION_FAILED,
            "Knowledge extraction LLM generation failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc


def _create_item_from_candidate(
    db: Session,
    candidate: ExtractedKnowledgeItem,
    request: KnowledgeExtractionRequest,
    chunks: list[DocumentChunk],
) -> KnowledgeItem:
    chunk_by_id = {chunk.id: chunk for chunk in chunks}
    candidate_chunks = [chunk_by_id[chunk_id] for chunk_id in candidate.source_chunk_ids]
    source_document_id = candidate_chunks[0].document_id
    payload = KnowledgeItemCreate(
        item_type=candidate.item_type,
        title=candidate.title,
        content=candidate.content,
        structured_data=candidate.structured_data,
        entities=candidate.entities,
        parameters=candidate.parameters,
        conditions=candidate.conditions,
        confidence=candidate.confidence,
        status="draft",
        source_document_id=source_document_id,
        source_chunk_ids=candidate.source_chunk_ids,
        created_by=request.created_by,
    )
    return knowledge_item_service.create_knowledge_item(db, payload)


def _build_duplicate_skip(
    candidate: ExtractedKnowledgeItem,
    chunks: list[DocumentChunk],
) -> KnowledgeExtractionSkippedDuplicate:
    chunk_by_id = {chunk.id: chunk for chunk in chunks}
    source_document_id = chunk_by_id[candidate.source_chunk_ids[0]].document_id
    content_hash = knowledge_item_service.compute_content_hash(
        candidate.item_type,
        source_document_id,
        candidate.title,
        candidate.content,
    )
    return KnowledgeExtractionSkippedDuplicate(
        item_type=candidate.item_type,
        title=candidate.title,
        content_hash=content_hash,
        source_document_id=source_document_id,
        reason="duplicate",
    )


def _validate_extraction_settings(settings: Any) -> None:
    _normalize_max_chunks(None, settings)
    _normalize_max_chars(settings)
    default_status = str(getattr(settings, "knowledge_extraction_default_status", "draft") or "").strip().lower()
    if default_status != "draft":
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "Knowledge extraction default status must be draft.",
            detail={"knowledge_extraction_default_status": default_status},
            status_code=400,
        )


def _normalize_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    if normalized not in {"document", "chunks"}:
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "Knowledge extraction mode must be document or chunks.",
            detail={"mode": mode},
            status_code=400,
        )
    return normalized


def _normalize_item_types(item_types: list[str] | None) -> list[str] | None:
    if item_types is None:
        return None
    normalized: list[str] = []
    for item_type in item_types:
        value = str(item_type or "").strip()
        if value not in KNOWLEDGE_ITEM_TYPES:
            raise BusinessError(
                KNOWLEDGE_ITEM_CONFIG_INVALID,
                "Knowledge extraction item_type is invalid.",
                detail={"item_type": value},
                status_code=400,
            )
        normalized.append(value)
    if not normalized:
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "Knowledge extraction item_types must not be empty.",
            status_code=400,
        )
    return normalized


def _normalize_max_chunks(requested_max_chunks: int | None, settings: Any) -> int:
    try:
        settings_max = int(getattr(settings, "knowledge_extraction_max_chunks", 20))
    except (TypeError, ValueError) as exc:
        raise _config_error("knowledge_extraction_max_chunks must be an integer.", exc) from exc
    if settings_max <= 0 or settings_max > 50:
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "knowledge_extraction_max_chunks must be between 1 and 50.",
            detail={"knowledge_extraction_max_chunks": settings_max},
            status_code=400,
        )

    if requested_max_chunks is None:
        return settings_max
    try:
        max_chunks = int(requested_max_chunks)
    except (TypeError, ValueError) as exc:
        raise _config_error("max_chunks must be an integer.", exc) from exc
    if max_chunks <= 0 or max_chunks > settings_max:
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "max_chunks must be greater than 0 and less than or equal to configured max.",
            detail={"max_chunks": max_chunks, "configured_max_chunks": settings_max},
            status_code=400,
        )
    return max_chunks


def _normalize_max_chars(settings: Any) -> int:
    try:
        max_chars = int(getattr(settings, "knowledge_extraction_max_chars", 12000))
    except (TypeError, ValueError) as exc:
        raise _config_error("knowledge_extraction_max_chars must be an integer.", exc) from exc
    if max_chars <= 0:
        raise BusinessError(
            KNOWLEDGE_ITEM_CONFIG_INVALID,
            "knowledge_extraction_max_chars must be greater than 0.",
            detail={"knowledge_extraction_max_chars": max_chars},
            status_code=400,
        )
    return max_chars


def _config_error(message: str, exc: Exception) -> BusinessError:
    return BusinessError(
        KNOWLEDGE_ITEM_CONFIG_INVALID,
        message,
        detail={"error_type": exc.__class__.__name__},
        status_code=400,
    )
