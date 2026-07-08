from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.errors import (
    KNOWLEDGE_ITEM_DUPLICATE,
    KNOWLEDGE_ITEM_CONFIG_INVALID,
    KNOWLEDGE_ITEM_EXTRACTION_FAILED,
    KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED,
    KNOWLEDGE_ITEM_INVALID_STATUS,
    KNOWLEDGE_ITEM_INVALID_TRANSITION,
    KNOWLEDGE_ITEM_NOT_FOUND,
    KNOWLEDGE_ITEM_REVIEW_FAILED,
    KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND,
    KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND,
    KNOWLEDGE_ITEM_VALIDATION_FAILED,
    KNOWLEDGE_ITEM_VERSION_FAILED,
    LLM_CONFIG_INVALID,
    LLM_GENERATION_FAILED,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    BusinessError,
)
from app.db.session import get_db
from app.schemas.common import ApiResponse
from app.schemas.knowledge_item import (
    KnowledgeItemChunksData,
    KnowledgeItemChunkData,
    KnowledgeItemCreate,
    KnowledgeItemData,
    KnowledgeExtractionData,
    KnowledgeExtractionLlmInfo,
    KnowledgeExtractionRequest,
    KnowledgeItemListData,
    KnowledgeItemReviewData,
    KnowledgeItemReviewRequest,
    KnowledgeItemReviewsData,
    KnowledgeItemReviseRequest,
    KnowledgeItemUpdate,
    KnowledgeItemVersionData,
    KnowledgeItemVersionsData,
)
from app.services import knowledge_extraction as knowledge_extraction_service
from app.services import knowledge_items as knowledge_item_service


router = APIRouter(prefix="/knowledge-items", tags=["knowledge-items"])

DbSession = Annotated[Session, Depends(get_db)]

ERROR_STATUS_CODES = {
    KNOWLEDGE_ITEM_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    KNOWLEDGE_ITEM_INVALID_STATUS: status.HTTP_400_BAD_REQUEST,
    KNOWLEDGE_ITEM_INVALID_TRANSITION: status.HTTP_409_CONFLICT,
    KNOWLEDGE_ITEM_VALIDATION_FAILED: status.HTTP_400_BAD_REQUEST,
    KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    KNOWLEDGE_ITEM_DUPLICATE: status.HTTP_409_CONFLICT,
    KNOWLEDGE_ITEM_VERSION_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    KNOWLEDGE_ITEM_REVIEW_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    KNOWLEDGE_ITEM_EXTRACTION_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    KNOWLEDGE_ITEM_CONFIG_INVALID: status.HTTP_400_BAD_REQUEST,
    LLM_CONFIG_INVALID: status.HTTP_400_BAD_REQUEST,
    LLM_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    LLM_TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
    LLM_GENERATION_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def business_error_response(error: BusinessError) -> JSONResponse:
    payload = ApiResponse[None](success=False, data=None, error=error.to_api_error())
    return JSONResponse(
        status_code=ERROR_STATUS_CODES.get(error.code, error.status_code),
        content=payload.model_dump(mode="json"),
    )


@router.get("", response_model=ApiResponse[KnowledgeItemListData])
def read_knowledge_items(
    db: DbSession,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    item_type: str | None = None,
    source_document_id: UUID | None = None,
    source_filename: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiResponse[KnowledgeItemListData] | JSONResponse:
    try:
        result = knowledge_item_service.list_knowledge_items(
            db,
            status=status_filter,
            item_type=item_type,
            source_document_id=source_document_id,
            source_filename=source_filename,
            limit=limit,
            offset=offset,
        )
        data = KnowledgeItemListData(
            items=[KnowledgeItemData.from_item(item) for item in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        )
        return ApiResponse[KnowledgeItemListData].ok(data)
    except BusinessError as error:
        return business_error_response(error)


@router.post("", response_model=ApiResponse[KnowledgeItemData], status_code=status.HTTP_201_CREATED)
def create_knowledge_item(
    request: KnowledgeItemCreate,
    db: DbSession,
) -> ApiResponse[KnowledgeItemData] | JSONResponse:
    try:
        item = knowledge_item_service.create_knowledge_item(db, request)
        return ApiResponse[KnowledgeItemData].ok(KnowledgeItemData.from_item(item))
    except BusinessError as error:
        return business_error_response(error)


@router.post("/extract", response_model=ApiResponse[KnowledgeExtractionData])
def extract_knowledge_items(
    request: KnowledgeExtractionRequest,
    db: DbSession,
) -> ApiResponse[KnowledgeExtractionData] | JSONResponse:
    try:
        result = knowledge_extraction_service.extract_knowledge_items(db, request)
        data = KnowledgeExtractionData(
            items=[KnowledgeItemData.from_item(item) for item in result.items],
            created=result.created,
            skipped_duplicates=result.skipped_duplicates,
            status=result.status,
            auto_submit=result.auto_submit,
            llm=KnowledgeExtractionLlmInfo(provider=result.llm_provider, model=result.llm_model),
        )
        return ApiResponse[KnowledgeExtractionData].ok(data)
    except BusinessError as error:
        return business_error_response(error)


@router.get("/{item_id}", response_model=ApiResponse[KnowledgeItemData])
def read_knowledge_item(
    db: DbSession,
    item_id: UUID,
) -> ApiResponse[KnowledgeItemData] | JSONResponse:
    try:
        item = knowledge_item_service.get_knowledge_item(db, item_id)
        return ApiResponse[KnowledgeItemData].ok(KnowledgeItemData.from_item(item))
    except BusinessError as error:
        return business_error_response(error)


@router.patch("/{item_id}", response_model=ApiResponse[KnowledgeItemData])
def update_knowledge_item(
    request: KnowledgeItemUpdate,
    db: DbSession,
    item_id: UUID,
) -> ApiResponse[KnowledgeItemData] | JSONResponse:
    try:
        item = knowledge_item_service.update_knowledge_item(db, item_id, request)
        return ApiResponse[KnowledgeItemData].ok(KnowledgeItemData.from_item(item))
    except BusinessError as error:
        return business_error_response(error)


@router.get("/{item_id}/chunks", response_model=ApiResponse[KnowledgeItemChunksData])
def read_knowledge_item_chunks(
    db: DbSession,
    item_id: UUID,
) -> ApiResponse[KnowledgeItemChunksData] | JSONResponse:
    try:
        chunks = knowledge_item_service.get_knowledge_item_chunks(db, item_id)
        data = KnowledgeItemChunksData(
            items=[KnowledgeItemChunkData.model_validate(chunk) for chunk in chunks],
            total=len(chunks),
        )
        return ApiResponse[KnowledgeItemChunksData].ok(data)
    except BusinessError as error:
        return business_error_response(error)


@router.get("/{item_id}/versions", response_model=ApiResponse[KnowledgeItemVersionsData])
def read_knowledge_item_versions(
    db: DbSession,
    item_id: UUID,
) -> ApiResponse[KnowledgeItemVersionsData] | JSONResponse:
    try:
        versions = knowledge_item_service.get_knowledge_item_versions(db, item_id)
        data = KnowledgeItemVersionsData(
            items=[KnowledgeItemVersionData.model_validate(version) for version in versions],
            total=len(versions),
        )
        return ApiResponse[KnowledgeItemVersionsData].ok(data)
    except BusinessError as error:
        return business_error_response(error)


@router.post("/{item_id}/submit", response_model=ApiResponse[KnowledgeItemData])
def submit_knowledge_item(
    request: KnowledgeItemReviewRequest,
    db: DbSession,
    item_id: UUID,
) -> ApiResponse[KnowledgeItemData] | JSONResponse:
    try:
        item = knowledge_item_service.submit_knowledge_item(
            db,
            item_id,
            reviewer=request.reviewer,
            review_comment=request.review_comment,
        )
        return ApiResponse[KnowledgeItemData].ok(KnowledgeItemData.from_item(item))
    except BusinessError as error:
        return business_error_response(error)


@router.post("/{item_id}/approve", response_model=ApiResponse[KnowledgeItemData])
def approve_knowledge_item(
    request: KnowledgeItemReviewRequest,
    db: DbSession,
    item_id: UUID,
) -> ApiResponse[KnowledgeItemData] | JSONResponse:
    try:
        item = knowledge_item_service.approve_knowledge_item(
            db,
            item_id,
            reviewer=request.reviewer,
            review_comment=request.review_comment,
        )
        return ApiResponse[KnowledgeItemData].ok(KnowledgeItemData.from_item(item))
    except BusinessError as error:
        return business_error_response(error)


@router.post("/{item_id}/reject", response_model=ApiResponse[KnowledgeItemData])
def reject_knowledge_item(
    request: KnowledgeItemReviewRequest,
    db: DbSession,
    item_id: UUID,
) -> ApiResponse[KnowledgeItemData] | JSONResponse:
    try:
        item = knowledge_item_service.reject_knowledge_item(
            db,
            item_id,
            reviewer=request.reviewer,
            review_comment=request.review_comment,
        )
        return ApiResponse[KnowledgeItemData].ok(KnowledgeItemData.from_item(item))
    except BusinessError as error:
        return business_error_response(error)


@router.post("/{item_id}/deprecate", response_model=ApiResponse[KnowledgeItemData])
def deprecate_knowledge_item(
    request: KnowledgeItemReviewRequest,
    db: DbSession,
    item_id: UUID,
) -> ApiResponse[KnowledgeItemData] | JSONResponse:
    try:
        item = knowledge_item_service.deprecate_knowledge_item(
            db,
            item_id,
            reviewer=request.reviewer,
            review_comment=request.review_comment,
        )
        return ApiResponse[KnowledgeItemData].ok(KnowledgeItemData.from_item(item))
    except BusinessError as error:
        return business_error_response(error)


@router.get("/{item_id}/reviews", response_model=ApiResponse[KnowledgeItemReviewsData])
def read_knowledge_item_reviews(
    db: DbSession,
    item_id: UUID,
) -> ApiResponse[KnowledgeItemReviewsData] | JSONResponse:
    try:
        reviews = knowledge_item_service.list_knowledge_item_reviews(db, item_id)
        data = KnowledgeItemReviewsData(
            items=[KnowledgeItemReviewData.model_validate(review) for review in reviews],
            total=len(reviews),
        )
        return ApiResponse[KnowledgeItemReviewsData].ok(data)
    except BusinessError as error:
        return business_error_response(error)


@router.post("/{item_id}/revise", response_model=ApiResponse[KnowledgeItemData])
def revise_knowledge_item(
    request: KnowledgeItemReviseRequest,
    db: DbSession,
    item_id: UUID,
) -> ApiResponse[KnowledgeItemData] | JSONResponse:
    try:
        item = knowledge_item_service.revise_knowledge_item(
            db,
            item_id,
            created_by=request.created_by,
            change_reason=request.change_reason,
        )
        return ApiResponse[KnowledgeItemData].ok(KnowledgeItemData.from_item(item))
    except BusinessError as error:
        return business_error_response(error)
