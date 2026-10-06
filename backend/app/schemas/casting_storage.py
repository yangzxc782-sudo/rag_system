"""Public metadata only; object keys and local execution paths stay private."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import BusinessError


class CastingIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_path: str = Field(max_length=512)
    error_code: str = Field(max_length=512)
    message: str = Field(max_length=512)


class CastingErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["file", "admission", "capacity", "integrity", "engine", "system"]
    code: str = Field(pattern=r"^CASTING_[A-Z0-9_]{1,80}$")
    message: str = Field(max_length=512)
    retryable: bool = False
    run_id: UUID | None = None
    issues: list[CastingIssue] = Field(default_factory=list, max_length=100)


class CastingStorageError(BusinessError):
    def __init__(self, code: str, message: str, *, status: int = 503, category: str = "system",
                 issues=None, retryable: bool = False, run_id: UUID | None = None):
        self.safe_detail = CastingErrorDetail(code=code, category=category, message=message,
                                             issues=issues or [], retryable=retryable, run_id=run_id)
        super().__init__(code, message, detail=self.safe_detail.model_dump(mode="json"), status_code=status)


class CastingInputFileView(BaseModel):
    file_id: UUID
    thread_id: UUID
    request_id: UUID
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    storage_state: Literal["pending", "ready"]
    admission_passed: bool
    created_at: datetime


class CastingInputFilesView(BaseModel):
    items: list[CastingInputFileView]
    next_before_id: UUID | None = None
    reusable_input: CastingInputFileView | None = None


class CastingRunView(BaseModel):
    run_id: UUID
    thread_id: UUID
    turn_id: UUID
    input_file_id: UUID
    input_sha256: str
    rule_id: str
    rule_version: str
    rule_sha256: str
    engine_id: str
    engine_version: str
    engine_sha256: str
    status: Literal["pending", "running", "persisting", "succeeded", "no_feasible_candidate", "admission_failed", "engine_failed", "timed_out", "interrupted"]
    execution_no: int
    result_file_id: UUID | None
    result_sha256: str | None
    candidate_count: int | None
    recommended_candidate_id: str | None
    error: CastingErrorDetail | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def input_view(row) -> CastingInputFileView:
    return CastingInputFileView(file_id=row.id, thread_id=row.session_id, request_id=row.upload_request_id,
        **{name: getattr(row, name) for name in ("original_filename", "content_type", "size_bytes", "sha256",
           "storage_state", "admission_passed", "created_at")})


def run_view(row) -> CastingRunView:
    return CastingRunView(run_id=row.id, thread_id=row.session_id,
        **{name: getattr(row, name) for name in CastingRunView.model_fields if name not in {"run_id", "thread_id"}})
