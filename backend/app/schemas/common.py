from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel


DataT = TypeVar("DataT")


class ApiError(BaseModel):
    code: str
    message: str
    detail: Any | None = None


class ApiResponse(BaseModel, Generic[DataT]):
    success: bool
    data: DataT | None = None
    error: ApiError | None = None

    @classmethod
    def ok(cls, data: DataT) -> "ApiResponse[DataT]":
        return cls(success=True, data=data, error=None)

    @classmethod
    def fail(cls, error: ApiError) -> "ApiResponse[None]":
        return ApiResponse[None](success=False, data=None, error=error)
