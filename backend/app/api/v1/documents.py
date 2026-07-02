from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.errors import (
    DOCUMENT_ALREADY_PARSED,
    DOCUMENT_CHUNK_CONFIG_INVALID,
    DOCUMENT_EMBEDDINGS_ALREADY_GENERATED,
    DOCUMENT_NOT_FOUND,
    DOCUMENT_NOT_PARSED,
    DOCUMENT_PARSE_FAILED,
    DOCUMENT_PARSER_UNAVAILABLE,
    DOCUMENT_SOURCE_FILE_NOT_FOUND,
    DOCUMENT_UPLOAD_FAILED,
    EMBEDDING_CONFIG_INVALID,
    EMBEDDING_DEPENDENCY_MISSING,
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_GENERATION_FAILED,
    EMBEDDING_MODEL_LOAD_FAILED,
    EMBEDDING_MODEL_PATH_NOT_FOUND,
    FILE_TOO_LARGE,
    INVALID_FILE_TYPE,
    MINIO_BUCKET_NOT_FOUND,
    MINIO_SERVICE_UNAVAILABLE,
    BusinessError,
)
from app.db.session import get_db
from app.schemas.common import ApiResponse
from app.schemas.document import DocumentDetail, DocumentListData, DocumentRead, DocumentUploadData
from app.schemas.document_chunk import (
    DocumentChunkListData,
    DocumentChunkRead,
    DocumentChunkStats,
    DocumentEmbeddingData,
    DocumentEmbeddingStatusData,
    DocumentParseData,
)
from app.services.document_parsing import list_document_chunks, parse_document
from app.services.documents import create_document_from_upload, get_document_by_id, list_documents
from app.services.embeddings import generate_document_embeddings, get_document_embedding_status

router = APIRouter(prefix="/documents", tags=["documents"])

DbSession = Annotated[Session, Depends(get_db)]
UploadFileField = Annotated[UploadFile, File(...)]

ERROR_STATUS_CODES = {
    INVALID_FILE_TYPE: status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    FILE_TOO_LARGE: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    DOCUMENT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    DOCUMENT_ALREADY_PARSED: status.HTTP_409_CONFLICT,
    DOCUMENT_NOT_PARSED: status.HTTP_409_CONFLICT,
    DOCUMENT_EMBEDDINGS_ALREADY_GENERATED: status.HTTP_409_CONFLICT,
    DOCUMENT_CHUNK_CONFIG_INVALID: status.HTTP_400_BAD_REQUEST,
    DOCUMENT_PARSER_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    DOCUMENT_SOURCE_FILE_NOT_FOUND: status.HTTP_503_SERVICE_UNAVAILABLE,
    DOCUMENT_PARSE_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    EMBEDDING_CONFIG_INVALID: status.HTTP_400_BAD_REQUEST,
    EMBEDDING_MODEL_PATH_NOT_FOUND: status.HTTP_503_SERVICE_UNAVAILABLE,
    EMBEDDING_DEPENDENCY_MISSING: status.HTTP_503_SERVICE_UNAVAILABLE,
    EMBEDDING_MODEL_LOAD_FAILED: status.HTTP_503_SERVICE_UNAVAILABLE,
    EMBEDDING_DIMENSION_MISMATCH: status.HTTP_500_INTERNAL_SERVER_ERROR,
    EMBEDDING_GENERATION_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
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


@router.post("/{document_id}/parse", response_model=ApiResponse[DocumentParseData])
def parse_document_endpoint(db: DbSession, document_id: UUID) -> ApiResponse[DocumentParseData] | JSONResponse:
    try:
        result = parse_document(db, document_id)
        return ApiResponse[DocumentParseData].ok(DocumentParseData.model_validate(result))
    except BusinessError as error:
        return business_error_response(error)


@router.get("/{document_id}/chunks", response_model=ApiResponse[DocumentChunkListData])
def read_document_chunks(
    db: DbSession,
    document_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiResponse[DocumentChunkListData] | JSONResponse:
    try:
        result = list_document_chunks(db, document_id, limit=limit, offset=offset)
        data = DocumentChunkListData(
            items=[DocumentChunkRead.from_chunk(item) for item in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
            stats=DocumentChunkStats.model_validate(result.stats),
        )
        return ApiResponse[DocumentChunkListData].ok(data)
    except BusinessError as error:
        return business_error_response(error)


@router.post("/{document_id}/embeddings", response_model=ApiResponse[DocumentEmbeddingData])
def generate_document_embeddings_endpoint(
    db: DbSession,
    document_id: UUID,
) -> ApiResponse[DocumentEmbeddingData] | JSONResponse:
    try:
        result = generate_document_embeddings(db, document_id)
        return ApiResponse[DocumentEmbeddingData].ok(DocumentEmbeddingData.model_validate(result))
    except BusinessError as error:
        return business_error_response(error)


@router.get("/{document_id}/embedding-status", response_model=ApiResponse[DocumentEmbeddingStatusData])
def read_document_embedding_status(
    db: DbSession,
    document_id: UUID,
) -> ApiResponse[DocumentEmbeddingStatusData] | JSONResponse:
    try:
        result = get_document_embedding_status(db, document_id)
        return ApiResponse[DocumentEmbeddingStatusData].ok(DocumentEmbeddingStatusData.model_validate(result))
    except BusinessError as error:
        return business_error_response(error)


@router.get("/{document_id}", response_model=ApiResponse[DocumentDetail])
def read_document(db: DbSession, document_id: UUID) -> ApiResponse[DocumentDetail] | JSONResponse:
    try:
        document = get_document_by_id(db, document_id)
        return ApiResponse[DocumentDetail].ok(DocumentDetail.model_validate(document))
    except BusinessError as error:
        return business_error_response(error)
