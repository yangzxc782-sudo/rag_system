from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import certifi
from minio import Minio
from minio.deleteobjects import DeleteError, DeleteObject
from minio.error import S3Error
from urllib3 import PoolManager, Timeout
from urllib3.util.retry import Retry

from app.core.config import get_settings
from app.core.errors import (
    DOCUMENT_SOURCE_FILE_NOT_FOUND,
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


def get_document_deletion_minio_client(*, settings: Any | None = None) -> Minio:
    """Create a bounded, zero-SDK-retry client for deletion primitives."""

    settings = settings or get_settings()
    timeout_seconds = int(settings.document_deletion_storage_timeout_seconds)
    http_client = PoolManager(
        timeout=Timeout(connect=timeout_seconds, read=timeout_seconds),
        maxsize=10,
        cert_reqs="CERT_REQUIRED" if settings.minio_secure else "CERT_NONE",
        ca_certs=certifi.where(),
        retries=Retry(total=0, connect=0, read=0, redirect=0, status=0),
    )
    return Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_root_user,
        secret_key=settings.minio_root_password,
        secure=settings.minio_secure,
        http_client=http_client,
    )


@dataclass(frozen=True, slots=True)
class MinioObjectVersion:
    object_key: str
    version_id: str | None
    is_delete_marker: bool


def list_minio_object_versions(
    *,
    client: Minio,
    bucket_name: str,
    prefix: str,
) -> Iterator[MinioObjectVersion]:
    for item in client.list_objects(
        bucket_name,
        prefix=prefix,
        recursive=True,
        include_version=True,
    ):
        if item.object_name is None:
            continue
        yield MinioObjectVersion(
            object_key=item.object_name,
            version_id=item.version_id,
            is_delete_marker=bool(item.is_delete_marker),
        )


def remove_minio_object_versions(
    *,
    client: Minio,
    bucket_name: str,
    versions: Iterable[MinioObjectVersion],
) -> Iterator[DeleteError]:
    return client.remove_objects(
        bucket_name,
        (
            DeleteObject(version.object_key, version.version_id)
            for version in versions
        ),
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


def get_object_bytes_from_minio(
    *,
    bucket_name: str,
    object_key: str,
    client: Minio | None = None,
    max_bytes: int | None = None,
) -> bytes:
    storage_client = client or get_minio_client()
    ensure_bucket_exists(storage_client, bucket_name)
    response = None

    try:
        response = storage_client.get_object(bucket_name=bucket_name, object_name=object_key)
        content = response.read() if max_bytes is None else response.read(max_bytes + 1)
        if max_bytes is not None and len(content) > max_bytes:
            raise ValueError("Object exceeds the caller's byte budget")
        return content
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject"}:
            raise BusinessError(
                DOCUMENT_SOURCE_FILE_NOT_FOUND,
                "MinIO 原始文件不存在，无法读取。",
                detail={
                    "bucket": bucket_name,
                    "object_key": object_key,
                    "minio_error": exc.code,
                },
                status_code=503,
            ) from exc

        raise BusinessError(
            MINIO_SERVICE_UNAVAILABLE,
            "MinIO 服务不可用，无法读取原始文件。",
            detail={
                "bucket": bucket_name,
                "object_key": object_key,
                "minio_error": exc.code,
            },
            status_code=503,
        ) from exc
    except Exception as exc:
        raise BusinessError(
            MINIO_SERVICE_UNAVAILABLE,
            "MinIO 服务不可用，无法读取原始文件。",
            detail={
                "bucket": bucket_name,
                "object_key": object_key,
                "error_type": exc.__class__.__name__,
            },
            status_code=503,
        ) from exc
    finally:
        if response is not None:
            response.close()
            response.release_conn()
