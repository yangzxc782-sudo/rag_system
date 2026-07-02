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
    NO_EMBEDDED_CHUNKS,
    VECTOR_SEARCH_FAILED,
    BusinessError,
)
from app.db.session import get_db
from app.schemas.common import ApiResponse
from app.schemas.search import VectorSearchData, VectorSearchRequest
from app.services.vector_search import vector_search_chunks


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
    VECTOR_SEARCH_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    NO_EMBEDDED_CHUNKS: status.HTTP_409_CONFLICT,
}


def business_error_response(error: BusinessError) -> JSONResponse:
    payload = ApiResponse[None](success=False, data=None, error=error.to_api_error())
    return JSONResponse(
        status_code=ERROR_STATUS_CODES.get(error.code, error.status_code),
        content=payload.model_dump(mode="json"),
    )


@router.post("/vector", response_model=ApiResponse[VectorSearchData])
def vector_search_endpoint(
    request: VectorSearchRequest,
    db: DbSession,
) -> ApiResponse[VectorSearchData] | JSONResponse:
    try:
        result = vector_search_chunks(
            db,
            query=request.query,
            limit=request.limit,
            document_id=request.document_id,
        )
        return ApiResponse[VectorSearchData].ok(VectorSearchData.model_validate(result))
    except BusinessError as error:
        return business_error_response(error)
