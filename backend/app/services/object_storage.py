from __future__ import annotations

from io import BytesIO

from minio import Minio
from minio.error import S3Error

from app.core.config import get_settings
from app.core.errors import (
    DOCUMENT_UPLOAD_FAILED,
    MINIO_BUCKET_NOT_FOUND,
    MINIO_SERVICE_UNAVAILABLE,
    BusinessError,
)


def get_minio_client() -> Minio:
    settings = get_settings()
    return Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_root_user,
        secret_key=settings.minio_root_password,
        secure=settings.minio_secure,
    )


def ensure_bucket_exists(client: Minio, bucket_name: str) -> None:
    try:
        bucket_exists = client.bucket_exists(bucket_name)
    except S3Error as exc:
        raise BusinessError(
            MINIO_SERVICE_UNAVAILABLE,
            "MinIO 服务不可用，无法检查 bucket。",
            detail={"bucket": bucket_name, "minio_error": exc.code},
            status_code=503,
        ) from exc
    except Exception as exc:
        raise BusinessError(
            MINIO_SERVICE_UNAVAILABLE,
            "MinIO 服务不可用，无法检查 bucket。",
            detail={"bucket": bucket_name, "error_type": exc.__class__.__name__},
            status_code=503,
        ) from exc

    if not bucket_exists:
        raise BusinessError(
            MINIO_BUCKET_NOT_FOUND,
            f"MinIO bucket '{bucket_name}' 不存在。",
            detail={"bucket": bucket_name},
            status_code=503,
        )


def upload_bytes_to_minio(
    *,
    bucket_name: str,
    object_key: str,
    content: bytes,
    content_type: str | None = None,
    client: Minio | None = None,
) -> None:
    storage_client = client or get_minio_client()
    ensure_bucket_exists(storage_client, bucket_name)

    try:
        storage_client.put_object(
            bucket_name=bucket_name,
            object_name=object_key,
            data=BytesIO(content),
            length=len(content),
            content_type=content_type or "application/octet-stream",
        )
    except S3Error as exc:
        raise BusinessError(
            DOCUMENT_UPLOAD_FAILED,
            "文件上传到 MinIO 失败。",
            detail={
                "bucket": bucket_name,
                "object_key": object_key,
                "minio_error": exc.code,
            },
            status_code=502,
        ) from exc
    except Exception as exc:
        raise BusinessError(
            DOCUMENT_UPLOAD_FAILED,
            "文件上传到 MinIO 失败。",
            detail={
                "bucket": bucket_name,
                "object_key": object_key,
                "error_type": exc.__class__.__name__,
            },
            status_code=502,
        ) from exc
