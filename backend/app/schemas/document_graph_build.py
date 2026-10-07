from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class GraphBuildCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_version: UUID
    request_id: UUID


class GraphBuildAdvance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    retry: bool = False


class GraphBuildStatus(BaseModel):
    graph_build_id: UUID
    document_id: UUID
    source_version: UUID
    graph_id: str
    status: str
    job_status: str
    stage: str
    unit_counts: dict[str, int]
    anchor_count: int | None
    entity_count: int | None
    relationship_count: int | None
    error_code: str | None
    attempt_count: int
    max_attempts: int
    lease_expires_at: datetime | None
    can_advance: bool
    next_stage: str | None
