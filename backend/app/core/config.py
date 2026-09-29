from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
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
    pdf_cleaning_enabled: bool = False
    pdf_cleaning_profile: Literal["auto", "gb_zh", "iso_en"] = "auto"
    pdf_cleaning_backfill_enabled: bool = True

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

    # Runtime credentials must be provisioned with database-level read-only
    # privileges. READ access mode alone is not an authorization boundary.
    graph_retrieval_enabled: bool = False
    neo4j_uri: str | None = Field(default=None, repr=False)
    neo4j_database: str | None = None
    neo4j_username: str | None = Field(default=None, repr=False)
    neo4j_password: SecretStr | None = Field(default=None, repr=False)
    graph_retrieval_max_anchors: int = Field(default=8, gt=0)
    graph_retrieval_max_entities_per_anchor: int = Field(default=200, gt=0)
    graph_retrieval_max_relationships_per_anchor: int = Field(default=400, gt=0)
    graph_retrieval_query_timeout_seconds: float = Field(default=2, gt=0, allow_inf_nan=False)
    graph_retrieval_total_budget_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    neo4j_connection_timeout_seconds: float = Field(default=1, gt=0, allow_inf_nan=False)
    neo4j_connection_acquisition_timeout_seconds: float = Field(default=1, gt=0, allow_inf_nan=False)

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
    rag_graph_context_max_chars: int = Field(default=50000, ge=0)
    rag_require_citations: bool = True
    rag_no_context_message: str = "当前知识库中未检索到足够依据，无法可靠回答该问题。"
    rag_system_prompt_name: str = "casting_rag_default"

    # Phase 13: opt in only after the separately authorized 0010 migration.
    conversation_enabled: bool = False
    conversation_graph_version: Literal["phase13_m2_v1", "phase13_m3_v2"] = "phase13_m3_v2"
    conversation_answer_max_input_tokens: int = Field(default=8192, ge=1024, le=100000)
    conversation_answer_graph_tokens: int = Field(default=1024, ge=0, le=50000)
    conversation_answer_safety_tokens: int = Field(default=1024, ge=128, le=8192)
    # Optional deployment-verified provider window; no tokenizer/window discovery
    # exists in the current Provider API. The input estimate is always enforced.
    conversation_model_context_window: int | None = Field(default=None, ge=2048)
    conversation_checkpoint_pool_min_size: int = Field(default=1, ge=1, le=16)
    conversation_checkpoint_pool_max_size: int = Field(default=8, ge=2, le=32)
    conversation_checkpoint_timeout_seconds: float = Field(default=5, gt=0, le=60, allow_inf_nan=False)
    conversation_history_max_turns: int = Field(default=6, ge=1, le=6)
    conversation_history_max_bytes: int = Field(default=12000, ge=128, le=64000)
    conversation_history_max_estimated_tokens: int = Field(default=4096, ge=128, le=64000)
    conversation_rewrite_max_input_bytes: int = Field(default=20000, ge=1024, le=64000)
    conversation_rewrite_max_estimated_tokens: int = Field(default=8192, ge=1024, le=64000)
    conversation_question_max_bytes: int = Field(default=6000, ge=128, le=6000)
    conversation_rewrite_max_output_bytes: int = Field(default=8192, ge=512, le=16000)
    conversation_rewrite_max_tokens: int = Field(default=768, ge=64, le=2048)
    conversation_rewrite_timeout_seconds: float = Field(default=15, gt=0, le=60, allow_inf_nan=False)
    conversation_rewrite_temperature: float = Field(default=0.0, ge=0, le=0.2, allow_inf_nan=False)

    knowledge_extraction_max_chunks: int = 20
    knowledge_extraction_max_chars: int = 12000
    knowledge_extraction_default_status: str = "draft"

    reranker_enabled: bool = False
    reranker_provider: str = "local_transformers"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_model_path: str = Field(
        default="D:/rag_system/models/bge-reranker-v2-m3", repr=False,
    )
    # Deprecated compatibility input; never used for candidate/result counts.
    reranker_top_k: int = 8
    reranker_device: str = "cuda"
    reranker_dtype: str = "fp16"
    # M5 selects the production profile. Missing values keep the provider unavailable.
    reranker_candidate_limit: int | None = Field(default=None, gt=0)
    reranker_batch_size: int | None = Field(default=None, gt=0)
    reranker_max_length: int | None = Field(default=None, gt=0)
    reranker_timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @field_validator("neo4j_uri")
    @classmethod
    def validate_neo4j_uri(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        uri = urlsplit(value.strip())
        if (uri.scheme not in {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}
                or not uri.hostname or uri.username is not None or uri.password is not None
                or uri.query or uri.fragment or uri.path not in {"", "/"}):
            raise ValueError("neo4j_uri must be a credential-free Bolt/Neo4j endpoint")
        if uri.port is not None and uri.port <= 0:
            raise ValueError("neo4j_uri port must be positive")
        return value.strip()

    @model_validator(mode="after")
    def validate_search_settings(self) -> "Settings":
        if self.conversation_checkpoint_pool_min_size > self.conversation_checkpoint_pool_max_size:
            raise ValueError("conversation checkpoint pool min_size exceeds max_size")
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
