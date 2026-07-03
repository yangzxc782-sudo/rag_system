from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
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
    upload_allowed_extensions: str = ".pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.bmp,.tif,.tiff,.webp,.txt,.md,.csv"
    # content_type is auxiliary metadata only. Leave this empty by default so browser-specific
    # or application/octet-stream values do not block an allowed extension.
    upload_allowed_content_types: str = ""
    backend_cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    document_parser: str = "simple"
    chunk_size_chars: int = 1000
    chunk_overlap_chars: int = 100
    mineru_endpoint: str = ""
    mineru_timeout_seconds: int = 60

    embedding_provider: str = "local_qwen3"
    embedding_model: str = "Qwen3-Embedding-0.6B"
    embedding_model_path: str = "D:/rag_system/models/Qwen3-Embedding-0.6B"
    embedding_device: str = "auto"
    embedding_batch_size: int = 8
    embedding_normalize: bool = True
    embedding_dim: int = 1024
    embedding_local_files_only: bool = True
    embedding_query_instruction: str = (
        "Given a search query about casting process knowledge, "
        "retrieve relevant document chunks that answer the query."
    )
    embedding_use_query_instruction: bool = True

    search_engine_provider: str = "opensearch"
    search_index_name: str = "casting_chunks_v1"
    search_index_alias: str = "casting_chunks_current"
    search_engine_url: str = "http://localhost:9200"
    search_engine_username: str = ""
    search_engine_password: str = ""
    search_engine_verify_ssl: bool = False
    search_engine_timeout_seconds: int = 30
    search_engine_max_retries: int = 3
    search_index_batch_size: int = 100
    search_vector_space: str = "cosine"
    search_content_analyzer: str = "ik_max_word"
    search_query_analyzer: str = "ik_smart"

    hybrid_keyword_weight: float = 0.5
    hybrid_vector_weight: float = 0.5
    hybrid_rrf_k: int = 60
    hybrid_keyword_top_k: int = 50
    hybrid_vector_top_k: int = 50
    hybrid_final_limit: int = 10

    @model_validator(mode="after")
    def validate_search_settings(self) -> "Settings":
        self.search_vector_space = self.search_vector_space.strip().lower()
        if self.search_vector_space != "cosine":
            raise ValueError("search_vector_space must be cosine in phase 5")
        if self.hybrid_keyword_weight == 0 and self.hybrid_vector_weight == 0:
            raise ValueError("hybrid_keyword_weight and hybrid_vector_weight cannot both be 0")
        if self.hybrid_rrf_k <= 0:
            raise ValueError("hybrid_rrf_k must be greater than 0")
        if self.hybrid_final_limit <= 0:
            raise ValueError("hybrid_final_limit must be greater than 0")
        if self.hybrid_keyword_top_k < self.hybrid_final_limit:
            raise ValueError("hybrid_keyword_top_k must be greater than or equal to hybrid_final_limit")
        if self.hybrid_vector_top_k < self.hybrid_final_limit:
            raise ValueError("hybrid_vector_top_k must be greater than or equal to hybrid_final_limit")
        return self

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
