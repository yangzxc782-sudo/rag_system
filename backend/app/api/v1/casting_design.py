"""Session-bound JSON upload and result reads; no public execute/path endpoint."""
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import Response

from app.api.v1.conversations import ConversationRoute, error_response
from app.schemas.casting_design import MAX_INPUT_BYTES
from app.schemas.casting_storage import CastingInputFilesView, CastingInputFileView, CastingRunView, CastingStorageError
from app.schemas.common import ApiResponse


MAX_MULTIPART_BYTES = MAX_INPUT_BYTES + 64 * 1024


class CastingRoute(ConversationRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            if request.method == "POST":
                length = request.headers.get("content-length")
                if length is not None:
                    try:
                        if not 0 <= int(length) <= MAX_MULTIPART_BYTES:
                            return error_response("CASTING_FILE_TOO_LARGE", 413)
                    except ValueError:
                        return error_response("CASTING_REQUEST_INVALID", 422)
                chunks = []
                consumed = 0
                # Bound the entire request *before* FastAPI's multipart parser.
                # Exceptions from receive() inside that parser become generic
                # parsing failures, so enforce the 413 response here directly.
                async for chunk in request.stream():
                    consumed += len(chunk)
                    if consumed > MAX_MULTIPART_BYTES:
                        return error_response("CASTING_FILE_TOO_LARGE", 413)
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            return await original(request)
        return handler


router = APIRouter(prefix="/rag/sessions", tags=["casting-design"], route_class=CastingRoute)


def service(request):
    if not request.app.state.settings.casting_design_enabled:
        raise CastingStorageError("CASTING_FEATURE_DISABLED", "工程文件功能未启用", status=404)
    result = getattr(request.app.state, "casting_design", None)
    if result is None:
        raise CastingStorageError("CASTING_SERVICE_UNAVAILABLE", "工程文件服务暂不可用")
    return result


@router.post("/{thread_id}/casting-inputs", response_model=ApiResponse[CastingInputFileView])
def upload_input(thread_id: UUID, request: Request, request_id: UUID = Form(...), file: UploadFile = File(...)):
    try:
        current = service(request)
        raw = file.file.read(MAX_INPUT_BYTES + 1)
        return ApiResponse.ok(current.files.upload(thread_id, request_id, file.filename or "", raw))
    finally:
        file.file.close()


@router.get("/{thread_id}/casting-inputs", response_model=ApiResponse[CastingInputFilesView])
def list_inputs(thread_id: UUID, request: Request, limit: int = Query(50, ge=1, le=50), before_id: UUID | None = None):
    return ApiResponse.ok(service(request).files.list_inputs(thread_id, limit=limit, before_id=before_id))


@router.get("/{thread_id}/casting-runs/{run_id}", response_model=ApiResponse[CastingRunView])
def get_run(thread_id: UUID, run_id: UUID, request: Request):
    return ApiResponse.ok(service(request).get_run(thread_id, run_id))


@router.get("/{thread_id}/casting-runs/{run_id}/recommendation")
def download(thread_id: UUID, run_id: UUID, request: Request):
    raw, sha = service(request).recommendation(thread_id, run_id)
    return Response(content=raw, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="recommendation-{run_id}.json"',
        "ETag": f'"sha256:{sha}"', "X-Content-SHA256": sha, "X-Content-Type-Options": "nosniff"})
