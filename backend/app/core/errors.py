from __future__ import annotations

from typing import Any

from app.schemas.common import ApiError, ApiResponse


INVALID_FILE_TYPE = "INVALID_FILE_TYPE"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
DOCUMENT_UPLOAD_FAILED = "DOCUMENT_UPLOAD_FAILED"
MINIO_BUCKET_NOT_FOUND = "MINIO_BUCKET_NOT_FOUND"
MINIO_SERVICE_UNAVAILABLE = "MINIO_SERVICE_UNAVAILABLE"


class BusinessError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: Any | None = None,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.status_code = status_code

    def to_api_error(self) -> ApiError:
        return ApiError(code=self.code, message=self.message, detail=self.detail)


def error_response(error: BusinessError) -> ApiResponse[None]:
    return ApiResponse[None](success=False, data=None, error=error.to_api_error())
