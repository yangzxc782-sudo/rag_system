from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from minio import Minio
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    DOCUMENT_NOT_FOUND,
    DOCUMENT_UPLOAD_FAILED,
    FILE_TOO_LARGE,
    INVALID_FILE_TYPE,
    BusinessError,
)
from app.models.document import Document
from app.services.object_storage import upload_bytes_to_minio


def validate_file_extension(filename: str) -> str:
    settings = get_settings()
    extension = Path(filename).suffix.lower()

    if extension not in settings.upload_allowed_extension_set:
        raise BusinessError(
            INVALID_FILE_TYPE,
            "不支持的文件类型。",
            detail={
                "filename": filename,
                "extension": extension,
                "allowed_extensions": sorted(settings.upload_allowed_extension_set),
            },
            status_code=400,
        )

    return extension


def validate_content_type(content_type: str | None) -> None:
    if not content_type:
        return

    settings = get_settings()
    allowed_content_types = settings.upload_allowed_content_type_set
    normalized_content_type = content_type.split(";", maxsplit=1)[0].strip().lower()

    if allowed_content_types and normalized_content_type not in allowed_content_types:
        raise BusinessError(
            INVALID_FILE_TYPE,
            "上传文件的 content_type 不在允许范围内。",
            detail={
                "content_type": content_type,
                "allowed_content_types": sorted(allowed_content_types),
            },
            status_code=400,
        )


def validate_file_size(file_size: int) -> None:
    settings = get_settings()

    if file_size > settings.upload_max_file_size_bytes:
        raise BusinessError(
            FILE_TOO_LARGE,
            "上传文件超过大小限制。",
            detail={
                "file_size": file_size,
                "max_file_size": settings.upload_max_file_size_bytes,
            },
            status_code=413,
        )


def calculate_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def generate_document_object_key(
    *,
    document_id: UUID,
    original_extension: str,
    uploaded_at: datetime | None = None,
) -> str:
    timestamp = uploaded_at or datetime.now(timezone.utc)
    return f"raw/{timestamp:%Y/%m}/{document_id}{original_extension.lower()}"


def create_document_from_upload(
    db: Session,
    *,
    original_filename: str,
    content: bytes,
    content_type: str | None = None,
    storage_client: Minio | None = None,
) -> Document:
    settings = get_settings()
    extension = validate_file_extension(original_filename)
    validate_content_type(content_type)

    file_size = len(content)
    validate_file_size(file_size)

    file_hash = calculate_sha256(content)
    document_id = uuid4()
    object_key = generate_document_object_key(
        document_id=document_id,
        original_extension=extension,
    )

    upload_bytes_to_minio(
        bucket_name=settings.minio_bucket,
        object_key=object_key,
        content=content,
        content_type=content_type,
        client=storage_client,
    )

    document = Document(
        id=document_id,
        original_filename=original_filename,
        bucket_name=settings.minio_bucket,
        object_key=object_key,
        file_type=extension,
        mime_type=content_type,
        file_size=file_size,
        file_hash=file_hash,
        process_status="uploaded",
    )

    try:
        db.add(document)
        db.commit()
        db.refresh(document)
    except SQLAlchemyError as exc:
        db.rollback()
        raise BusinessError(
            DOCUMENT_UPLOAD_FAILED,
            "文档元数据写入失败。",
            detail={"filename": original_filename, "object_key": object_key},
            status_code=500,
        ) from exc

    return document


def list_documents(db: Session, *, limit: int, offset: int) -> tuple[list[Document], int]:
    total = db.scalar(select(func.count()).select_from(Document)) or 0
    items = list(
        db.scalars(
            select(Document)
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset),
        ).all(),
    )
    return items, total


def get_document_by_id(db: Session, document_id: UUID) -> Document:
    document = db.get(Document, document_id)

    if document is None:
        raise BusinessError(
            DOCUMENT_NOT_FOUND,
            "文档不存在。",
            detail={"document_id": str(document_id)},
            status_code=404,
        )

    return document
