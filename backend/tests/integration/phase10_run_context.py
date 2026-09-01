from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID


_RUN_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{8,32}$")
_DOCUMENT_TYPES = frozenset({"core", "recovery", "other", "frontend"})


class Phase10IntegrationRunContextError(ValueError):
    """Raised when a Phase 10 integration run identity is invalid."""


@dataclass(frozen=True, slots=True)
class Phase10IntegrationRunContext:
    """Pure, credential-free identity for one dedicated integration run."""

    run_token: str

    def __post_init__(self) -> None:
        if not _RUN_TOKEN_PATTERN.fullmatch(self.run_token):
            raise Phase10IntegrationRunContextError(
                "Phase 10 integration run token must be 8-32 lowercase hex characters."
            )

    @property
    def postgres_database(self) -> str:
        return f"phase10_m7b_{self.run_token}"

    @property
    def migration_database(self) -> str:
        return f"phase10_m7b_migration_{self.run_token}"

    @property
    def restore_database(self) -> str:
        return f"phase10_m7b_restore_{self.run_token}"

    @property
    def minio_bucket(self) -> str:
        return f"phase10-m7b-{self.run_token}"

    @property
    def minio_locked_bucket(self) -> str:
        return f"{self.minio_bucket}-locked"

    @property
    def minio_missing_bucket(self) -> str:
        return f"{self.minio_bucket}-missing"

    @property
    def opensearch_index(self) -> str:
        return f"phase10-m7b-{self.run_token}-v1"

    @property
    def opensearch_rollover_index(self) -> str:
        return f"phase10-m7b-{self.run_token}-v2"

    @property
    def opensearch_alias(self) -> str:
        return f"phase10-m7b-{self.run_token}-current"

    @property
    def document_prefix(self) -> str:
        return f"phase10-hard-delete-{self.run_token}-"

    def build_document_namespace(
        self,
        document_id: UUID,
        *,
        document_type: str = "core",
    ) -> str:
        if document_type not in _DOCUMENT_TYPES:
            raise Phase10IntegrationRunContextError(
                "Phase 10 integration document type is not allowed."
            )
        subtype = "" if document_type == "core" else f"{document_type}-"
        return f"{self.document_prefix}{subtype}{document_id}"

    def owns_document_namespace(self, namespace: str, document_id: UUID) -> bool:
        return any(
            namespace
            == self.build_document_namespace(
                document_id,
                document_type=document_type,
            )
            for document_type in _DOCUMENT_TYPES
        )

    def validate_configured_resources(
        self,
        *,
        postgres_database: str,
        migration_database: str,
        restore_database: str,
        minio_bucket: str,
        opensearch_index: str,
        opensearch_alias: str,
    ) -> None:
        configured = {
            "PostgreSQL database": postgres_database,
            "migration database": migration_database,
            "restore database": restore_database,
            "MinIO bucket": minio_bucket,
            "OpenSearch index": opensearch_index,
            "OpenSearch alias": opensearch_alias,
        }
        expected = {
            "PostgreSQL database": self.postgres_database,
            "migration database": self.migration_database,
            "restore database": self.restore_database,
            "MinIO bucket": self.minio_bucket,
            "OpenSearch index": self.opensearch_index,
            "OpenSearch alias": self.opensearch_alias,
        }
        for field, actual in configured.items():
            if actual != expected[field]:
                raise Phase10IntegrationRunContextError(
                    f"Phase 10 integration run context {field} mismatch."
                )

    def validate_storage_extension_resources(
        self,
        *,
        minio_locked_bucket: str,
        minio_missing_bucket: str,
        opensearch_rollover_index: str,
    ) -> None:
        configured = {
            "locked bucket": minio_locked_bucket,
            "missing bucket": minio_missing_bucket,
            "rollover index": opensearch_rollover_index,
        }
        expected = {
            "locked bucket": self.minio_locked_bucket,
            "missing bucket": self.minio_missing_bucket,
            "rollover index": self.opensearch_rollover_index,
        }
        for field, actual in configured.items():
            if actual != expected[field]:
                raise Phase10IntegrationRunContextError(
                    f"Phase 10 integration run context {field} mismatch."
                )

    def safe_summary(self) -> dict[str, str]:
        return {
            "run_token": self.run_token,
            "postgres_database": self.postgres_database,
            "migration_database": self.migration_database,
            "restore_database": self.restore_database,
            "minio_bucket": self.minio_bucket,
            "minio_locked_bucket": self.minio_locked_bucket,
            "minio_missing_bucket": self.minio_missing_bucket,
            "opensearch_index": self.opensearch_index,
            "opensearch_rollover_index": self.opensearch_rollover_index,
            "opensearch_alias": self.opensearch_alias,
            "document_prefix": self.document_prefix,
        }
