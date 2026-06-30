from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.errors import (
    DOCUMENT_NOT_FOUND,
    DOCUMENT_UPLOAD_FAILED,
    FILE_TOO_LARGE,
    INVALID_FILE_TYPE,
    MINIO_BUCKET_NOT_FOUND,
    MINIO_SERVICE_UNAVAILABLE,
    BusinessError,
)
from app.db.session import get_db
from app.schemas.common import ApiResponse
from app.schemas.document import DocumentDetail, DocumentListData, DocumentRead, DocumentUploadData
from app.services.documents import create_document_from_upload, get_document_by_id, list_documents

router = APIRouter(prefix="/documents", tags=["documents"])

DbSession = Annotated[Session, Depends(get_db)]
UploadFileField = Annotated[UploadFile, File(...)]

ERROR_STATUS_CODES = {
    INVALID_FILE_TYPE: status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    FILE_TOO_LARGE: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    DOCUMENT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    MINIO_BUCKET_NOT_FOUND: status.HTTP_503_SERVICE_UNAVAILABLE,
    MINIO_SERVICE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    DOCUMENT_UPLOAD_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def business_error_response(error: BusinessError) -> JSONResponse:
    payload = ApiResponse[None](success=False, data=None, error=error.to_api_error())
    return JSONResponse(
        status_code=ERROR_STATUS_CODES.get(error.code, error.status_code),
        content=payload.model_dump(mode="json"),
    )


@router.post(
    "",
    response_model=ApiResponse[DocumentUploadData],
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(db: DbSession, file: UploadFileField) -> ApiResponse[DocumentUploadData] | JSONResponse:
    try:
        content = await file.read()
        document = create_document_from_upload(
            db,
            original_filename=file.filename or "",
            content=content,
            content_type=file.content_type,
        )
        return ApiResponse[DocumentUploadData].ok(DocumentUploadData.model_validate(document))
    except BusinessError as error:
        return business_error_response(error)
    finally:
        await file.close()


@router.get("", response_model=ApiResponse[DocumentListData])
def read_documents(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiResponse[DocumentListData] | JSONResponse:
    try:
        items, total = list_documents(db, limit=limit, offset=offset)
        data = DocumentListData(
            items=[DocumentRead.model_validate(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )
        return ApiResponse[DocumentListData].ok(data)
    except BusinessError as error:
        return business_error_response(error)


@router.get("/{document_id}", response_model=ApiResponse[DocumentDetail])
def read_document(db: DbSession, document_id: UUID) -> ApiResponse[DocumentDetail] | JSONResponse:
    try:
        document = get_document_by_id(db, document_id)
        return ApiResponse[DocumentDetail].ok(DocumentDetail.model_validate(document))
    except BusinessError as error:
        return business_error_response(error)
