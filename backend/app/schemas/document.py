from __future__ import annotations

from datetime import datetime
from typing import Any
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
    deletion_status: str = "normal"
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
    deletion_status: str = "normal"
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


class DocumentParseRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    parser_provider: str
    parser_version: str | None = None
    parse_mode: str | None = None
    status: str
    is_active: bool
    input_file_key: str | None = None
    output_prefix: str | None = None
    output_markdown_key: str | None = None
    output_json_key: str | None = None
    output_markdown_status: str
    output_json_status: str
    failure_status_persisted: bool | None = None
    page_count: int | None = None
    block_count: int | None = None
    asset_count: int | None = None
    error_message: str | None = None
    source_metadata_summary: dict[str, Any]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class DocumentParseRunListData(BaseModel):
    items: list[DocumentParseRunRead]
    total: int
    limit: int
    offset: int


class DocumentParseStatusRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: UUID
    process_status: str
    latest_parse_run: DocumentParseRunRead | None = None
    active_parse_run: DocumentParseRunRead | None = None


class DocumentBlockRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    parse_run_id: UUID
    block_index: int
    block_key: str | None = None
    block_type: str
    page_start: int | None = None
    page_end: int | None = None
    bbox: dict[str, Any] | list[Any] | None = None
    text: str | None = None
    markdown: str | None = None
    html: str | None = None
    latex: str | None = None
    caption: str | None = None
    parent_block_key: str | None = None
    section_path: list[str]
    confidence: float | None = None
    source_metadata_summary: dict[str, Any]
    content_truncated: bool
    created_at: datetime


class PaginatedDocumentBlocks(BaseModel):
    items: list[DocumentBlockRead]
    total: int
    limit: int
    offset: int


class DocumentAssetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    parse_run_id: UUID
    asset_type: str
    page_number: int | None = None
    asset_key: str
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    caption: str | None = None
    source_block_key: str | None = None
    source_metadata_summary: dict[str, Any]
    created_at: datetime


class PaginatedDocumentAssets(BaseModel):
    items: list[DocumentAssetRead]
    total: int
    limit: int
    offset: int
