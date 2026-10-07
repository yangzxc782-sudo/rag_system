from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.ingestion.sequential_chunker import SegmentationConfig


class ProcessDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    config: SegmentationConfig = Field(default_factory=SegmentationConfig)


class ResumeProcessingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: SegmentationConfig = Field(default_factory=SegmentationConfig)


class ProcessingJobRead(BaseModel):
    job_id: UUID
    document_id: UUID
    request_id: UUID
    operation: Literal["process", "rechunk"]
    status: str
    stage: str
    source_version: UUID | None
    graph_build_id: UUID | None
    chunk_set_id: UUID | None
    parse_run_id: UUID | None
    attempt_count: int
    max_attempts: int
    lease_expires_at: datetime | None
    last_error_code: str | None
    can_retry: bool
    can_cancel: bool
    cancel_requested: bool
    requires_io_reconciliation: bool
    managed: bool
    config: SegmentationConfig | None
    created_at: datetime
    updated_at: datetime
    graph_status: str | None = None
    chunk_set_status: str | None = None
    unit_count: int | None = None
    completed_units: int = 0
    chunk_count: int | None = None
    embedded_count: int = 0


class ProcessingJobList(BaseModel):
    items: list[ProcessingJobRead]
    total: int
    executor_enabled: bool
    search_enabled: bool
    can_process: bool
