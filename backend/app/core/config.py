from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "铸型工艺知识库 RAG 系统"
    app_env: str = "local"
    api_v1_prefix: str = "/api/v1"
    database_url: str = "postgresql+psycopg://rag_user:rag_password@localhost:5432/rag_system"

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = "rag_redis_password"

    minio_endpoint: str = "localhost:9000"
    minio_root_user: str = "rag_minio"
    minio_root_password: str = "rag_minio_password"
    minio_bucket: str = "rag-documents"
    minio_secure: bool = False

    upload_max_file_size_bytes: int = 52_428_800
    upload_allowed_extensions: str = ".pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.bmp,.tif,.tiff,.webp"
    # content_type is only an auxiliary check; extension and size limits remain the primary phase 2 gates.
    upload_allowed_content_types: str = (
        "application/pdf,"
        "application/msword,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "application/vnd.ms-excel,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
        "image/png,"
        "image/jpeg,"
        "image/bmp,"
        "image/tiff,"
        "image/webp"
    )
    backend_cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def upload_allowed_extension_set(self) -> set[str]:
        return {item.strip().lower() for item in self.upload_allowed_extensions.split(",") if item.strip()}

    @property
    def upload_allowed_content_type_set(self) -> set[str]:
        return {item.strip().lower() for item in self.upload_allowed_content_types.split(",") if item.strip()}

    @property
    def backend_cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.backend_cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
