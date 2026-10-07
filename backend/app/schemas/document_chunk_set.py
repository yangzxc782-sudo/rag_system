from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from app.ingestion.sequential_chunker import SegmentationConfig


class ChunkSetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_version: UUID
    graph_build_id: UUID
    request_id: UUID
    operation: Literal["process", "rechunk"] = "process"
    config: SegmentationConfig = Field(default_factory=SegmentationConfig)
    auto_run: bool = Field(default=False, strict=True)


class ChunkSetAdvance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry: bool = Field(default=False, strict=True)


class ChunkSetStatus(BaseModel):
    chunk_set_id: UUID
    document_id: UUID
    source_version: UUID
    graph_build_id: UUID
    request_id: UUID
    operation: str
    status: str
    job_status: str
    stage: str
    chunk_count: int | None
    embedding_counts: dict[str, int]
    segmentation_config: SegmentationConfig
    is_current: bool
    publication_revision: int
    index_name: str | None
    last_error_code: str | None
    lease_expires_at: datetime | None
    can_advance: bool
    search_enabled: bool
    job_id: UUID | None = None
    managed: bool = False
