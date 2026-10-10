from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.core.errors import (
    DOCUMENT_ALREADY_PARSED,
    DOCUMENT_CHUNK_CONFIG_INVALID,
    DOCUMENT_EMBEDDINGS_ALREADY_GENERATED,
    DOCUMENT_NOT_FOUND,
    DOCUMENT_NOT_PARSED,
    DOCUMENT_PARSE_FAILED,
    DOCUMENT_PARSE_RUN_NOT_FOUND,
    DOCUMENT_PARSER_UNAVAILABLE,
    DOCUMENT_SOURCE_FILE_NOT_FOUND,
    DOCUMENT_UPLOAD_FAILED,
    DOCUMENT_DELETION_EXECUTOR_DISABLED,
    DOCUMENT_DELETION_IN_PROGRESS,
    DOCUMENT_DELETION_NOT_FAILED,
    DOCUMENT_DELETION_RETRY_REQUIRED,
    DOCUMENT_DELETE_FAILED,
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
from app.schemas.document import (
    DocumentAssetRead,
    DocumentBlockRead,
    DocumentDetail,
    DocumentListData,
    DocumentParseRunListData,
    DocumentParseRunRead,
    DocumentParseStatusRead,
    DocumentRead,
    DocumentUploadData,
    PaginatedDocumentAssets,
    PaginatedDocumentBlocks,
)
from app.schemas.document_chunk import (
    DocumentChunkListData,
    DocumentChunkRead,
    DocumentChunkStats,
    DocumentEmbeddingData,
    DocumentEmbeddingStatusData,
    DocumentParseData,
)
from app.schemas.document_deletion import DocumentDeletionStatusData
from app.schemas.document_processing import ProcessDocumentRequest, ResumeProcessingRequest, ProcessingJobRead, ProcessingJobList
from app.services.document_processing import (
    request_processing, processing_status, list_processing_jobs, retry_processing, cancel_processing, manage_rechunk,
)
from app.schemas.document_graph_build import GraphBuildAdvance, GraphBuildCreate, GraphBuildStatus
from app.services.document_graph_builds import advance_graph_build, graph_build_status, prepare_graph_build
from app.schemas.document_chunk_set import ChunkSetCreate, ChunkSetAdvance, ChunkSetStatus, ChunkSetList
from app.services.document_chunk_sets import prepare_chunk_set, advance_chunk_set, chunk_set_status, list_chunk_sets
from app.services.document_deletion import (
    get_document_deletion_status,
    request_document_deletion,
    retry_document_deletion,
)
from app.services.document_parsing import list_document_chunks, parse_document
from app.services.document_assets import list_assets as list_document_assets
from app.services.document_blocks import list_blocks as list_document_blocks
from app.services.document_parse_runs import (
    get_parse_status as get_document_parse_status,
    list_parse_runs as list_document_parse_runs,
)
from app.services.documents import create_document_from_upload, get_document_by_id, list_documents
from app.services.embeddings import generate_document_embeddings, get_document_embedding_status

router = APIRouter(prefix="/documents", tags=["documents"])
logger = logging.getLogger(__name__)

DbSession = Annotated[Session, Depends(get_db)]
UploadFileField = Annotated[UploadFile, File(...)]

ERROR_STATUS_CODES = {
    INVALID_FILE_TYPE: status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    FILE_TOO_LARGE: status.HTTP_413_CONTENT_TOO_LARGE,
    DOCUMENT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    DOCUMENT_PARSE_RUN_NOT_FOUND: status.HTTP_404_NOT_FOUND,
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
    DOCUMENT_DELETION_EXECUTOR_DISABLED: status.HTTP_503_SERVICE_UNAVAILABLE,
    DOCUMENT_DELETION_IN_PROGRESS: status.HTTP_409_CONFLICT,
    DOCUMENT_DELETE_FAILED: status.HTTP_409_CONFLICT,
    DOCUMENT_DELETION_RETRY_REQUIRED: status.HTTP_409_CONFLICT,
    DOCUMENT_DELETION_NOT_FAILED: status.HTTP_409_CONFLICT,
}


def business_error_response(error: BusinessError) -> JSONResponse:
    payload = ApiResponse[None](success=False, data=None, error=error.to_api_error())
    return JSONResponse(
        status_code=ERROR_STATUS_CODES.get(error.code, error.status_code),
        content=payload.model_dump(mode="json"),
    )


def _wake_document_deletion_executor(request: Request) -> None:
    executor = getattr(request.app.state, "document_deletion_executor", None)
    if executor is not None:
        try:
            executor.wake()
        except Exception as exc:
            logger.warning(
                "Document deletion executor wake failed; durable job remains recoverable.",
                extra={"error_type": exc.__class__.__name__},
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


@router.delete(
    "/{document_id}",
    response_model=ApiResponse[DocumentDeletionStatusData],
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_document_endpoint(
    request: Request,
    db: DbSession,
    document_id: UUID,
) -> ApiResponse[DocumentDeletionStatusData] | JSONResponse | Response:
    try:
        result = request_document_deletion(
            db,
            document_id,
            settings=request.app.state.settings,
        )
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        _wake_document_deletion_executor(request)
        return ApiResponse[DocumentDeletionStatusData].ok(
            DocumentDeletionStatusData.model_validate(result)
        )
    except BusinessError as error:
        return business_error_response(error)


@router.get(
    "/{document_id}/deletion-status",
    response_model=ApiResponse[DocumentDeletionStatusData],
)
def read_document_deletion_status(
    db: DbSession,
    document_id: UUID,
) -> ApiResponse[DocumentDeletionStatusData] | JSONResponse | Response:
    try:
        result = get_document_deletion_status(db, document_id)
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return ApiResponse[DocumentDeletionStatusData].ok(
            DocumentDeletionStatusData.model_validate(result)
        )
    except BusinessError as error:
        return business_error_response(error)


@router.post(
    "/{document_id}/deletion/retry",
    response_model=ApiResponse[DocumentDeletionStatusData],
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_document_deletion_endpoint(
    request: Request,
    db: DbSession,
    document_id: UUID,
) -> ApiResponse[DocumentDeletionStatusData] | JSONResponse | Response:
    try:
        result = retry_document_deletion(
            db,
            document_id,
            settings=request.app.state.settings,
        )
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        _wake_document_deletion_executor(request)
        return ApiResponse[DocumentDeletionStatusData].ok(
            DocumentDeletionStatusData.model_validate(result)
        )
    except BusinessError as error:
        return business_error_response(error)


@router.post("/{document_id}/parse", response_model=ApiResponse[DocumentParseData])
def parse_document_endpoint(db: DbSession, document_id: UUID) -> ApiResponse[DocumentParseData] | JSONResponse:
    try:
        result = parse_document(db, document_id)
        return ApiResponse[DocumentParseData].ok(DocumentParseData.model_validate(result))
    except BusinessError as error:
        return business_error_response(error)


@router.get(
    "/{document_id}/parse-runs",
    response_model=ApiResponse[DocumentParseRunListData],
)
def read_document_parse_runs(
    db: DbSession,
    document_id: UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiResponse[DocumentParseRunListData] | JSONResponse:
    try:
        result = list_document_parse_runs(
            db,
            document_id,
            limit=limit,
            offset=offset,
        )
        data = DocumentParseRunListData(
            items=[
                DocumentParseRunRead.model_validate(item)
                for item in result.items
            ],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        )
        return ApiResponse[DocumentParseRunListData].ok(data)
    except BusinessError as error:
        return business_error_response(error)


@router.get(
    "/{document_id}/parse-status",
    response_model=ApiResponse[DocumentParseStatusRead],
)
def read_document_parse_status(
    db: DbSession,
    document_id: UUID,
) -> ApiResponse[DocumentParseStatusRead] | JSONResponse:
    try:
        result = get_document_parse_status(db, document_id)
        return ApiResponse[DocumentParseStatusRead].ok(
            DocumentParseStatusRead.model_validate(result)
        )
    except BusinessError as error:
        return business_error_response(error)


@router.get(
    "/{document_id}/blocks",
    response_model=ApiResponse[PaginatedDocumentBlocks],
)
def read_document_blocks(
    db: DbSession,
    document_id: UUID,
    parse_run_id: Annotated[UUID | None, Query()] = None,
    block_type: Annotated[str | None, Query(max_length=50)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiResponse[PaginatedDocumentBlocks] | JSONResponse:
    try:
        result = list_document_blocks(
            db,
            document_id,
            parse_run_id=parse_run_id,
            block_type=block_type,
            limit=limit,
            offset=offset,
        )
        data = PaginatedDocumentBlocks(
            items=[
                DocumentBlockRead.model_validate(item)
                for item in result.items
            ],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        )
        return ApiResponse[PaginatedDocumentBlocks].ok(data)
    except BusinessError as error:
        return business_error_response(error)


@router.get(
    "/{document_id}/assets",
    response_model=ApiResponse[PaginatedDocumentAssets],
)
def read_document_assets(
    db: DbSession,
    document_id: UUID,
    parse_run_id: Annotated[UUID | None, Query()] = None,
    asset_type: Annotated[str | None, Query(max_length=50)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiResponse[PaginatedDocumentAssets] | JSONResponse:
    try:
        result = list_document_assets(
            db,
            document_id,
            parse_run_id=parse_run_id,
            asset_type=asset_type,
            limit=limit,
            offset=offset,
        )
        data = PaginatedDocumentAssets(
            items=[
                DocumentAssetRead.model_validate(item)
                for item in result.items
            ],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        )
        return ApiResponse[PaginatedDocumentAssets].ok(data)
    except BusinessError as error:
        return business_error_response(error)


@router.get("/{document_id}/chunks", response_model=ApiResponse[DocumentChunkListData])
def read_document_chunks(
    db: DbSession,
    document_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    chunk_set_id: UUID | None = None,
) -> ApiResponse[DocumentChunkListData] | JSONResponse:
    try:
        result = list_document_chunks(db, document_id, limit=limit, offset=offset, chunk_set_id=chunk_set_id)
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


@router.post("/{document_id}/graph-builds", response_model=ApiResponse[GraphBuildStatus])
def create_graph_build_endpoint(
    db: DbSession, document_id: UUID, body: GraphBuildCreate,
) -> ApiResponse[GraphBuildStatus] | JSONResponse:
    try:
        data = prepare_graph_build(db, document_id, body.source_version, body.request_id)
        return ApiResponse[GraphBuildStatus].ok(GraphBuildStatus.model_validate(data))
    except BusinessError as error:
        return business_error_response(error)


@router.get("/{document_id}/graph-builds/{build_id}", response_model=ApiResponse[GraphBuildStatus])
def read_graph_build_endpoint(
    db: DbSession, document_id: UUID, build_id: UUID,
) -> ApiResponse[GraphBuildStatus] | JSONResponse:
    try:
        return ApiResponse[GraphBuildStatus].ok(GraphBuildStatus.model_validate(graph_build_status(db, document_id, build_id)))
    except BusinessError as error:
        return business_error_response(error)


@router.post("/{document_id}/graph-builds/{build_id}/advance", response_model=ApiResponse[GraphBuildStatus])
def advance_graph_build_endpoint(
    db: DbSession, document_id: UUID, build_id: UUID, body: GraphBuildAdvance,
) -> ApiResponse[GraphBuildStatus] | JSONResponse:
    try:
        data = advance_graph_build(db, document_id, build_id, retry=body.retry)
        return ApiResponse[GraphBuildStatus].ok(GraphBuildStatus.model_validate(data))
    except BusinessError as error:
        return business_error_response(error)


@router.post("/{document_id}/chunk-sets", response_model=ApiResponse[ChunkSetStatus])
def create_chunk_set_endpoint(request: Request, db: DbSession, document_id: UUID, body: ChunkSetCreate) -> ApiResponse[ChunkSetStatus] | JSONResponse:
    try:
        if body.auto_run:
            from app.services.document_processing import _enabled
            _enabled(request.app.state.settings, operation=body.operation)
        data = prepare_chunk_set(db, document_id, body.source_version, body.graph_build_id, body.request_id,
                                 body.config, operation=body.operation, settings=request.app.state.settings)
        if body.auto_run and not data["managed"] and data["job_status"] == "queued":
            manage_rechunk(db, document_id, data["job_id"], settings=request.app.state.settings)
            data = chunk_set_status(db, document_id, data["chunk_set_id"], settings=request.app.state.settings)
            _wake_processing(request)
        return ApiResponse[ChunkSetStatus].ok(ChunkSetStatus.model_validate(data))
    except BusinessError as exc:
        return business_error_response(exc)


@router.get("/{document_id}/chunk-sets", response_model=ApiResponse[ChunkSetList])
def read_chunk_sets_endpoint(db: DbSession, document_id: UUID,
                            limit: Annotated[int, Query(ge=1, le=100)] = 20,
                            offset: Annotated[int, Query(ge=0)] = 0) -> ApiResponse[ChunkSetList] | JSONResponse:
    try:
        return ApiResponse[ChunkSetList].ok(ChunkSetList.model_validate(list_chunk_sets(db, document_id, limit=limit, offset=offset)))
    except BusinessError as exc:
        return business_error_response(exc)


@router.get("/{document_id}/chunk-sets/{set_id}", response_model=ApiResponse[ChunkSetStatus])
def read_chunk_set_endpoint(db: DbSession, document_id: UUID, set_id: UUID) -> ApiResponse[ChunkSetStatus] | JSONResponse:
    try:
        return ApiResponse[ChunkSetStatus].ok(ChunkSetStatus.model_validate(chunk_set_status(db, document_id, set_id)))
    except BusinessError as exc:
        return business_error_response(exc)


@router.post("/{document_id}/chunk-sets/{set_id}/advance", response_model=ApiResponse[ChunkSetStatus])
def advance_chunk_set_endpoint(db: DbSession, document_id: UUID, set_id: UUID, body: ChunkSetAdvance) -> ApiResponse[ChunkSetStatus] | JSONResponse:
    try:
        return ApiResponse[ChunkSetStatus].ok(ChunkSetStatus.model_validate(advance_chunk_set(db, document_id, set_id, retry=body.retry)))
    except BusinessError as exc:
        return business_error_response(exc)


def _wake_processing(request):
    executor = getattr(request.app.state, "document_processing_executor", None)
    if executor:
        try:
            executor.wake()
        except Exception:
            logger.warning("Processing wake failed; durable task will be polled.")


@router.post("/{document_id}/process", response_model=ApiResponse[ProcessingJobRead], status_code=202)
def process_document_endpoint(request: Request, db: DbSession, document_id: UUID, body: ProcessDocumentRequest):
    try:
        data = request_processing(db, document_id, body.request_id, body.config, settings=request.app.state.settings)
        _wake_processing(request)
        return ApiResponse[ProcessingJobRead].ok(ProcessingJobRead.model_validate(data))
    except BusinessError as exc:
        return business_error_response(exc)


@router.get("/{document_id}/processing-jobs", response_model=ApiResponse[ProcessingJobList])
def read_processing_jobs(request: Request, db: DbSession, document_id: UUID,
                         limit: Annotated[int, Query(ge=1, le=100)] = 20):
    try:
        data = list_processing_jobs(db, document_id, settings=request.app.state.settings, limit=limit)
        return ApiResponse[ProcessingJobList].ok(ProcessingJobList.model_validate(data))
    except BusinessError as exc:
        return business_error_response(exc)


@router.get("/{document_id}/processing-jobs/{job_id}", response_model=ApiResponse[ProcessingJobRead])
def read_processing_job(db: DbSession, document_id: UUID, job_id: UUID):
    try:
        return ApiResponse[ProcessingJobRead].ok(ProcessingJobRead.model_validate(processing_status(db, document_id, job_id)))
    except BusinessError as exc:
        return business_error_response(exc)


@router.post("/{document_id}/processing-jobs/{job_id}/retry", response_model=ApiResponse[ProcessingJobRead], status_code=202)
def retry_processing_job(request: Request, db: DbSession, document_id: UUID, job_id: UUID):
    try:
        data = retry_processing(db, document_id, job_id, settings=request.app.state.settings)
        _wake_processing(request)
        return ApiResponse[ProcessingJobRead].ok(ProcessingJobRead.model_validate(data))
    except BusinessError as exc:
        return business_error_response(exc)


@router.post("/{document_id}/processing-jobs/{job_id}/cancel", response_model=ApiResponse[ProcessingJobRead])
def cancel_processing_job(request: Request, db: DbSession, document_id: UUID, job_id: UUID):
    try:
        data = cancel_processing(db, document_id, job_id)
        _wake_processing(request)
        return ApiResponse[ProcessingJobRead].ok(ProcessingJobRead.model_validate(data))
    except BusinessError as exc:
        return business_error_response(exc)


@router.post("/{document_id}/processing-jobs/{job_id}/resume", response_model=ApiResponse[ProcessingJobRead], status_code=202)
def resume_chunk_job(request: Request, db: DbSession, document_id: UUID, job_id: UUID, body: ResumeProcessingRequest):
    try:
        data = manage_rechunk(db, document_id, job_id, settings=request.app.state.settings, config=body.config)
        _wake_processing(request)
        return ApiResponse[ProcessingJobRead].ok(ProcessingJobRead.model_validate(data))
    except BusinessError as exc:
        return business_error_response(exc)


@router.get("/{document_id}", response_model=ApiResponse[DocumentDetail])
def read_document(db: DbSession, document_id: UUID) -> ApiResponse[DocumentDetail] | JSONResponse:
    try:
        document = get_document_by_id(db, document_id)
        return ApiResponse[DocumentDetail].ok(DocumentDetail.model_validate(document))
    except BusinessError as error:
        return business_error_response(error)
