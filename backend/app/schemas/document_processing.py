from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ingestion.block_chunker import BlockChunkerConfig


class ProcessDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    config: BlockChunkerConfig = Field(default_factory=BlockChunkerConfig)


class ResumeProcessingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Omission adopts the frozen request; an explicitly supplied config must match.
    config: BlockChunkerConfig | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self):
        if "config" in self.model_fields_set and self.config is None:
            raise ValueError("Omit config to reuse the frozen configuration; null is not supported")
        return self


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
    config: BlockChunkerConfig | None
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
    segmentation_defaults: BlockChunkerConfig
    segmentation_version: str
