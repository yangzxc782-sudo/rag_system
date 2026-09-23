from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.errors import (
    DOCUMENT_NOT_FOUND,
    EMBEDDING_CONFIG_INVALID,
    EMBEDDING_DEPENDENCY_MISSING,
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_GENERATION_FAILED,
    EMBEDDING_MODEL_LOAD_FAILED,
    EMBEDDING_MODEL_PATH_NOT_FOUND,
    HYBRID_SEARCH_CONFIG_INVALID,
    HYBRID_SEARCH_FAILED,
    NO_EMBEDDED_CHUNKS,
    SEARCH_ENGINE_CONFIG_INVALID,
    SEARCH_ENGINE_UNAVAILABLE,
    SEARCH_INDEX_CREATE_FAILED,
    SEARCH_INDEX_EMPTY,
    SEARCH_INDEX_MAPPING_MISMATCH,
    SEARCH_INDEX_NOT_FOUND,
    SEARCH_INDEX_REBUILD_FAILED,
    SEARCH_QUERY_EMPTY,
    BusinessError,
)
from app.db.session import get_db
from app.schemas.common import ApiResponse
from app.schemas.search import (
    SearchData,
    SearchIndexCreateData,
    SearchIndexRebuildData,
    SearchIndexRebuildRequest,
    SearchIndexStatusData,
    SearchRequest,
)
from app.services import hybrid_search as hybrid_search_service
from app.services import search_index as search_index_service


router = APIRouter(prefix="/search", tags=["search"])

DbSession = Annotated[Session, Depends(get_db)]

ERROR_STATUS_CODES = {
    DOCUMENT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    EMBEDDING_CONFIG_INVALID: status.HTTP_400_BAD_REQUEST,
    EMBEDDING_MODEL_PATH_NOT_FOUND: status.HTTP_503_SERVICE_UNAVAILABLE,
    EMBEDDING_DEPENDENCY_MISSING: status.HTTP_503_SERVICE_UNAVAILABLE,
    EMBEDDING_MODEL_LOAD_FAILED: status.HTTP_503_SERVICE_UNAVAILABLE,
    EMBEDDING_DIMENSION_MISMATCH: status.HTTP_500_INTERNAL_SERVER_ERROR,
    EMBEDDING_GENERATION_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    NO_EMBEDDED_CHUNKS: status.HTTP_409_CONFLICT,
    SEARCH_ENGINE_CONFIG_INVALID: status.HTTP_400_BAD_REQUEST,
    SEARCH_ENGINE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    SEARCH_INDEX_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    SEARCH_INDEX_EMPTY: status.HTTP_409_CONFLICT,
    SEARCH_INDEX_CREATE_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    SEARCH_INDEX_REBUILD_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    SEARCH_INDEX_MAPPING_MISMATCH: status.HTTP_409_CONFLICT,
    SEARCH_QUERY_EMPTY: status.HTTP_400_BAD_REQUEST,
    HYBRID_SEARCH_CONFIG_INVALID: status.HTTP_400_BAD_REQUEST,
    HYBRID_SEARCH_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def business_error_response(error: BusinessError) -> JSONResponse:
    payload = ApiResponse[None](success=False, data=None, error=error.to_api_error())
    return JSONResponse(
        status_code=ERROR_STATUS_CODES.get(error.code, error.status_code),
        content=payload.model_dump(mode="json"),
    )


@router.post("", response_model=ApiResponse[SearchData])
def hybrid_search_endpoint(
    request: SearchRequest,
    db: DbSession,
) -> ApiResponse[SearchData] | JSONResponse:
    try:
        result = hybrid_search_service.hybrid_search_chunks(
            db,
            query=request.query,
            limit=request.limit,
            document_id=request.document_id,
        )
        return ApiResponse[SearchData].ok(SearchData.model_validate(result))
    except BusinessError as error:
        return business_error_response(error)


@router.post("/index/create", response_model=ApiResponse[SearchIndexCreateData])
def create_search_index_endpoint(db: DbSession) -> ApiResponse[SearchIndexCreateData] | JSONResponse:
    try:
        result = search_index_service.create_or_update_search_index(db)
        return ApiResponse[SearchIndexCreateData].ok(SearchIndexCreateData.model_validate(result))
    except BusinessError as error:
        return business_error_response(error)


@router.post("/index/rebuild", response_model=ApiResponse[SearchIndexRebuildData])
def rebuild_search_index_endpoint(
    request: SearchIndexRebuildRequest,
    db: DbSession,
) -> ApiResponse[SearchIndexRebuildData] | JSONResponse:
    try:
        result = search_index_service.rebuild_search_index(
            db,
            scope=request.scope,
            document_id=request.document_id,
        )
        return ApiResponse[SearchIndexRebuildData].ok(SearchIndexRebuildData.model_validate(result))
    except BusinessError as error:
        return business_error_response(error)


@router.get("/index/status", response_model=ApiResponse[SearchIndexStatusData])
def search_index_status_endpoint(db: DbSession) -> ApiResponse[SearchIndexStatusData] | JSONResponse:
    try:
        result = search_index_service.get_search_index_status(db)
        return ApiResponse[SearchIndexStatusData].ok(SearchIndexStatusData.model_validate(result))
    except BusinessError as error:
        return business_error_response(error)
