from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.errors import (
    HYBRID_SEARCH_FAILED,
    LLM_ERROR_STATUS_CODES,
    RAG_ANSWER_FAILED,
    RAG_CONFIG_INVALID,
    RAG_QUERY_EMPTY,
    SEARCH_ENGINE_UNAVAILABLE,
    SEARCH_INDEX_NOT_FOUND,
    BusinessError,
)
from app.db.session import get_db
from app.schemas.common import ApiResponse
from app.schemas.rag import RagAskData, RagAskRequest
from app.services import rag as rag_service


router = APIRouter(prefix="/rag", tags=["rag"])

DbSession = Annotated[Session, Depends(get_db)]

ERROR_STATUS_CODES = {
    **LLM_ERROR_STATUS_CODES,
    RAG_QUERY_EMPTY: status.HTTP_400_BAD_REQUEST,
    RAG_CONFIG_INVALID: status.HTTP_400_BAD_REQUEST,
    RAG_ANSWER_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    SEARCH_ENGINE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    SEARCH_INDEX_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    HYBRID_SEARCH_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def business_error_response(error: BusinessError) -> JSONResponse:
    payload = ApiResponse[None](success=False, data=None, error=error.to_api_error())
    return JSONResponse(
        status_code=ERROR_STATUS_CODES.get(error.code, error.status_code),
        content=payload.model_dump(mode="json"),
    )


def validate_request(request: RagAskRequest) -> None:
    if not request.question.strip():
        raise BusinessError(
            RAG_QUERY_EMPTY,
            "Question must not be empty.",
            status_code=400,
        )
    if request.limit <= 0:
        raise BusinessError(
            RAG_CONFIG_INVALID,
            "RAG limit must be greater than 0.",
            detail={"limit": request.limit},
            status_code=400,
        )


@router.post("/ask", response_model=ApiResponse[RagAskData])
def rag_ask_endpoint(
    request: RagAskRequest,
    db: DbSession,
    http_request: Request,
) -> ApiResponse[RagAskData] | JSONResponse:
    try:
        validate_request(request)
        result = rag_service.answer_question(
            db,
            question=request.question,
            limit=request.limit,
            document_id=request.document_id,
            settings=getattr(http_request.app.state, "settings", None),
            graph_retrieval=getattr(http_request.app.state, "graph_retrieval", None),
        )
        return ApiResponse[RagAskData].ok(RagAskData.from_service_result(result))
    except BusinessError as error:
        return business_error_response(error)
    except Exception as exc:
        return business_error_response(
            BusinessError(
                RAG_ANSWER_FAILED,
                "RAG answer request failed.",
                detail={"error_type": exc.__class__.__name__},
                status_code=500,
            )
        )
