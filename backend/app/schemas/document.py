from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    original_filename: str
    file_type: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    file_hash: str | None = None
    process_status: str
    created_at: datetime
    updated_at: datetime


class DocumentDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    original_filename: str
    bucket_name: str
    object_key: str
    file_type: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    file_hash: str | None = None
    process_status: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class DocumentListData(BaseModel):
    items: list[DocumentRead]
    total: int
    limit: int
    offset: int


class DocumentUploadData(DocumentDetail):
    pass
