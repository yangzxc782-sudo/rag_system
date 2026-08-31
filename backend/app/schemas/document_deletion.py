from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DocumentDeletionStatusData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: UUID
    status: str
    step_attempts: int
    next_retry_at: datetime | None
    last_error_code: str | None
    updated_at: datetime
