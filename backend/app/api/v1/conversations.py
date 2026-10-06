"""Local-only conversation routes; legacy /rag/ask keeps its original contract."""
import re
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.core.errors import BusinessError, LLM_ERROR_STATUS_CODES
from app.schemas.common import ApiError, ApiResponse
from app.schemas.casting_storage import CastingStorageError
from app.schemas.conversations import (
    MessageHistoryResponse, RequestStatusResponse, SessionCreateRequest, SessionCreateResponse,
    SessionDetailResponse, SessionListResponse, SessionUpdateRequest, TurnCreateRequest, TurnCreateResponse,
)


# Internal ASGI capability, never derived from a header, CORS or a client UUID.
LOCAL_TRANSPORT = object()


def error_response(code, status, *, status_url=None):
    payload = ApiResponse.fail(ApiError(code=code, message="Conversation request could not be completed.",
                                      detail={"status_url": status_url} if status_url else None))
    return JSONResponse(status_code=status, content=payload.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


class ConversationRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            def status_url():
                rid = getattr(request.state, "conversation_request_id", None)
                sid = request.path_params.get("thread_id")
                return f"{request.app.state.settings.api_v1_prefix}/rag/sessions/{sid}/requests/{rid}" if rid and sid else None
            if not request.app.state.settings.conversation_enabled:
                return error_response("QA_FEATURE_DISABLED", 404)
            if request.scope.get("conversation.local_transport") is not LOCAL_TRANSPORT:
                return error_response("QA_LOCAL_TRANSPORT_REQUIRED", 503)
            if not request.scope.get("conversation.local_peer", False):
                return error_response("QA_LOCAL_ACCESS_ONLY", 403)
            if getattr(request.app.state, "conversations", None) is None:
                return error_response("QA_SERVICE_UNAVAILABLE", 503)
            try:
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except RequestValidationError:
                return error_response("QA_REQUEST_INVALID", 422)
            except CastingStorageError as exc:
                return JSONResponse(status_code=exc.status_code,
                    content=ApiResponse.fail(ApiError(code=exc.code, message=exc.message,
                        detail=exc.safe_detail.model_dump(mode="json"))).model_dump(mode="json"),
                    headers={"Cache-Control": "no-store"})
            except BusinessError as exc:
                code = {"QA_REQUEST_CONFLICT": "IDEMPOTENCY_CONFLICT", "QA_THREAD_BUSY": "THREAD_BUSY"}.get(exc.code, exc.code)
                code = code if re.fullmatch(r"[A-Z0-9_]{1,100}", code) else "QA_INTERNAL_ERROR"
                status = LLM_ERROR_STATUS_CODES.get(code, exc.status_code)
                return error_response(code, status, status_url=status_url())
            except Exception:
                return error_response("QA_SERVICE_UNAVAILABLE", 503, status_url=status_url())
        return handler


router = APIRouter(prefix="/rag/sessions", tags=["conversations"], route_class=ConversationRoute)


@router.post("", response_model=ApiResponse[SessionCreateResponse])
def create_session(body: SessionCreateRequest, request: Request):
    return ApiResponse.ok(request.app.state.conversations.create_session(body))


@router.get("", response_model=ApiResponse[SessionListResponse])
def list_sessions(request: Request, limit: int = Query(50, ge=1, le=50), cursor: str | None = Query(None, max_length=256)):
    return ApiResponse.ok(request.app.state.conversations.list_sessions(limit=limit, cursor=cursor))


@router.get("/{thread_id}", response_model=ApiResponse[SessionDetailResponse])
def get_session(thread_id: UUID, request: Request):
    return ApiResponse.ok(request.app.state.conversations.session_detail(thread_id))


@router.patch("/{thread_id}", response_model=ApiResponse[SessionCreateResponse])
def rename_session(thread_id: UUID, body: SessionUpdateRequest, request: Request):
    return ApiResponse.ok(request.app.state.conversations.rename_session(thread_id, body))


@router.get("/{thread_id}/messages", response_model=ApiResponse[MessageHistoryResponse])
def messages(thread_id: UUID, request: Request, limit: int = Query(50, ge=1, le=50),
             before_seq: int | None = Query(None, ge=1)):
    return ApiResponse.ok(request.app.state.conversations.messages(thread_id, limit=limit, before_seq=before_seq))


@router.post("/{thread_id}/turns", response_model=ApiResponse[TurnCreateResponse],
             responses={202: {"model": ApiResponse[RequestStatusResponse], "description": "Execution is still active"}})
def submit_turn(thread_id: UUID, body: TurnCreateRequest, request: Request):
    request.state.conversation_request_id = body.request_id
    status, result = request.app.state.conversations.submit(thread_id, body)
    return JSONResponse(status_code=status, content=ApiResponse.ok(result).model_dump(mode="json"),
                        headers={"Location": result.status_url, **({"Retry-After": "1"} if status == 202 else {})})


@router.get("/{thread_id}/requests/{request_id}", response_model=ApiResponse[RequestStatusResponse])
def request_status(thread_id: UUID, request_id: UUID, request: Request):
    return ApiResponse.ok(request.app.state.conversations.request_status(thread_id, request_id))
