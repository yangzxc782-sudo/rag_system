from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.ingestion.file_types import DOCUMENT_FILE_EXTENSIONS


BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    document_parser_provider: Literal["mineru_api"] = "mineru_api"
    mineru_api_base_url: str | None = None
    mineru_api_key: SecretStr | None = None
    mineru_api_timeout_seconds: int = 300
    mineru_api_poll_interval_seconds: int = 5
    mineru_api_max_poll_attempts: int = 120
    mineru_output_prefix: str = "parsed-assets"
    mineru_parse_mode: str = "auto"
    mineru_enable_ocr: bool = True
    mineru_save_intermediate: bool = True

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
    document_deletion_executor_enabled: bool = False
    document_deletion_max_step_attempts: int = 5
    document_deletion_retry_base_seconds: int = 5
    document_deletion_retry_max_seconds: int = 300
    document_deletion_lease_seconds: int = 120
    document_deletion_poll_interval_seconds: float = 5
    document_deletion_shutdown_grace_seconds: float = 10
    document_deletion_storage_timeout_seconds: int = 30
    document_deletion_heartbeat_object_interval: int = 25

    upload_max_file_size_bytes: int = 52_428_800
    upload_allowed_extensions: str = ".pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.bmp,.webp,.md"
    # content_type is auxiliary metadata only. Leave this empty by default so browser-specific
    # or application/octet-stream values do not block an allowed extension.
    upload_allowed_content_types: str = ""
    backend_cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # Shared sizing settings remain in use by the MinerU block-aware chunker.
    chunk_size_chars: int = 1000
    chunk_overlap_chars: int = 100

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

    llm_provider: str = "local"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "qwen3:8b"
    llm_api_key: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_timeout_seconds: int = 120
    llm_remote_base_url: str = ""
    llm_remote_api_key: SecretStr | None = None
    llm_remote_model: str = ""
    llm_remote_timeout_seconds: int = 60
    llm_remote_supports_json_mode: bool = False
    llm_remote_allow_insecure_http: bool = False

    rag_top_k: int = 8
    rag_context_max_chars: int = 12000
    rag_require_citations: bool = True
    rag_no_context_message: str = "当前知识库中未检索到足够依据，无法可靠回答该问题。"
    rag_system_prompt_name: str = "casting_rag_default"

    knowledge_extraction_max_chunks: int = 20
    knowledge_extraction_max_chars: int = 12000
    knowledge_extraction_default_status: str = "draft"

    reranker_enabled: bool = False
    reranker_provider: str = "local_qwen3"
    reranker_model: str = "Qwen3-Reranker-0.6B"
    reranker_model_path: str = "D:/rag_system/models/Qwen3-Reranker-0.6B"
    reranker_top_k: int = 8

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
        if self.rag_top_k <= 0:
            raise ValueError("rag_top_k must be greater than 0")
        if self.rag_context_max_chars <= 0:
            raise ValueError("rag_context_max_chars must be greater than 0")
        self.rag_no_context_message = self.rag_no_context_message.strip()
        if not self.rag_no_context_message:
            raise ValueError("rag_no_context_message must not be empty")
        if self.knowledge_extraction_max_chunks <= 0:
            raise ValueError("knowledge_extraction_max_chunks must be greater than 0")
        if self.knowledge_extraction_max_chunks > 50:
            raise ValueError("knowledge_extraction_max_chunks must be less than or equal to 50")
        if self.knowledge_extraction_max_chars <= 0:
            raise ValueError("knowledge_extraction_max_chars must be greater than 0")
        self.knowledge_extraction_default_status = self.knowledge_extraction_default_status.strip().lower()
        if self.knowledge_extraction_default_status != "draft":
            raise ValueError("knowledge_extraction_default_status must be draft")
        if self.document_deletion_storage_timeout_seconds <= 0:
            raise ValueError(
                "document_deletion_storage_timeout_seconds must be greater than 0"
            )
        if self.document_deletion_max_step_attempts <= 0:
            raise ValueError(
                "document_deletion_max_step_attempts must be greater than 0"
            )
        if self.document_deletion_retry_base_seconds <= 0:
            raise ValueError(
                "document_deletion_retry_base_seconds must be greater than 0"
            )
        if (
            self.document_deletion_retry_max_seconds
            < self.document_deletion_retry_base_seconds
        ):
            raise ValueError(
                "document_deletion_retry_max_seconds must be greater than or equal "
                "to document_deletion_retry_base_seconds"
            )
        if self.document_deletion_lease_seconds <= 0:
            raise ValueError("document_deletion_lease_seconds must be greater than 0")
        if (
            self.document_deletion_lease_seconds
            < 3 * self.document_deletion_storage_timeout_seconds
        ):
            raise ValueError(
                "document_deletion_lease_seconds must be at least three times "
                "document_deletion_storage_timeout_seconds"
            )
        if self.document_deletion_poll_interval_seconds <= 0:
            raise ValueError(
                "document_deletion_poll_interval_seconds must be greater than 0"
            )
        if self.document_deletion_shutdown_grace_seconds < 0:
            raise ValueError(
                "document_deletion_shutdown_grace_seconds must be greater than or equal to 0"
            )
        if self.document_deletion_heartbeat_object_interval <= 0:
            raise ValueError(
                "document_deletion_heartbeat_object_interval must be greater than 0"
            )
        if self.reranker_top_k <= 0:
            raise ValueError("reranker_top_k must be greater than 0")
        return self

    @property
    def upload_allowed_extension_set(self) -> set[str]:
        configured = {item.strip().lower() for item in self.upload_allowed_extensions.split(",") if item.strip()}
        return configured & DOCUMENT_FILE_EXTENSIONS

    @property
    def upload_allowed_content_type_set(self) -> set[str]:
        return {item.strip().lower() for item in self.upload_allowed_content_types.split(",") if item.strip()}

    @property
    def backend_cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.backend_cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
