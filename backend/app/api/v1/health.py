from typing import Any

from fastapi import APIRouter
from minio import Minio
from minio.error import S3Error
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    return {"status": "ok", "service": settings.app_name}


@router.get("/health/services")
def service_health() -> dict[str, Any]:
    services = {
        "postgresql": check_postgresql(),
        "redis": check_redis(),
        "minio": check_minio(),
    }
    statuses = {service["status"] for service in services.values()}
    overall_status = "failed" if "failed" in statuses else "warning" if "warning" in statuses else "ok"
    return {"status": overall_status, "services": services}


def check_postgresql() -> dict[str, str]:
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "detail": "PostgreSQL is reachable."}
    except SQLAlchemyError as exc:
        return {"status": "failed", "detail": f"PostgreSQL check failed: {exc.__class__.__name__}."}
    finally:
        engine.dispose()


def check_redis() -> dict[str, str]:
    settings = get_settings()
    client = Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        client.ping()
        return {"status": "ok", "detail": "Redis is reachable."}
    except RedisError as exc:
        return {"status": "failed", "detail": f"Redis check failed: {exc.__class__.__name__}."}
    finally:
        client.close()


def check_minio() -> dict[str, str]:
    settings = get_settings()
    client = Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_root_user,
        secret_key=settings.minio_root_password,
        secure=settings.minio_secure,
    )
    try:
        bucket_exists = client.bucket_exists(settings.minio_bucket)
    except S3Error as exc:
        if exc.code == "NoSuchBucket":
            return {
                "status": "warning",
                "detail": f"MinIO is reachable but bucket '{settings.minio_bucket}' does not exist.",
                "bucket": settings.minio_bucket,
            }
        return {"status": "failed", "detail": f"MinIO check failed: {exc.code}."}
    except Exception as exc:
        return {"status": "failed", "detail": f"MinIO check failed: {exc.__class__.__name__}."}

    if not bucket_exists:
        return {
            "status": "warning",
            "detail": f"MinIO is reachable but bucket '{settings.minio_bucket}' does not exist.",
            "bucket": settings.minio_bucket,
        }

    return {"status": "ok", "detail": "MinIO is reachable and bucket exists.", "bucket": settings.minio_bucket}
