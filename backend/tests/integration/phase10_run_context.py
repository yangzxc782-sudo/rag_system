from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from uuid import UUID


_RUN_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{8,32}$")
_DOCUMENT_TYPES = frozenset({"core", "recovery", "other", "frontend"})


class Phase10IntegrationRunContextError(ValueError):
    """Raised when a Phase 10 integration run identity is invalid."""


class Phase10ResourceDomain(str, Enum):
    VALIDATION = "validation"
    ROLLOUT = "rollout"


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
        return self.postgres_database_for(Phase10ResourceDomain.ROLLOUT)

    def postgres_database_for(self, domain: Phase10ResourceDomain) -> str:
        if domain is Phase10ResourceDomain.VALIDATION:
            return f"phase10_m7b_validation_{self.run_token}"
        if domain is Phase10ResourceDomain.ROLLOUT:
            return f"phase10_m7b_{self.run_token}"
        raise Phase10IntegrationRunContextError("Phase 10 resource domain is invalid.")

    @property
    def migration_database(self) -> str:
        return f"phase10_m7b_migration_{self.run_token}"

    @property
    def restore_database(self) -> str:
        return f"phase10_m7b_restore_{self.run_token}"

    @property
    def minio_bucket(self) -> str:
        return self.minio_bucket_for(Phase10ResourceDomain.ROLLOUT)

    def minio_bucket_for(self, domain: Phase10ResourceDomain) -> str:
        if domain is Phase10ResourceDomain.VALIDATION:
            return f"phase10-m7b-{self.run_token}-validation"
        if domain is Phase10ResourceDomain.ROLLOUT:
            return f"phase10-m7b-{self.run_token}"
        raise Phase10IntegrationRunContextError("Phase 10 resource domain is invalid.")

    @property
    def validation_minio_locked_bucket(self) -> str:
        return f"{self.minio_bucket_for(Phase10ResourceDomain.VALIDATION)}-locked"

    @property
    def validation_minio_missing_bucket(self) -> str:
        return f"{self.minio_bucket_for(Phase10ResourceDomain.VALIDATION)}-missing"

    @property
    def opensearch_index(self) -> str:
        return self.opensearch_index_for(Phase10ResourceDomain.ROLLOUT)

    def opensearch_index_for(self, domain: Phase10ResourceDomain) -> str:
        if domain is Phase10ResourceDomain.VALIDATION:
            return f"phase10-m7b-{self.run_token}-validation-v1"
        if domain is Phase10ResourceDomain.ROLLOUT:
            return f"phase10-m7b-{self.run_token}-v1"
        raise Phase10IntegrationRunContextError("Phase 10 resource domain is invalid.")

    @property
    def validation_opensearch_rollover_index(self) -> str:
        return f"phase10-m7b-{self.run_token}-validation-v2"

    @property
    def opensearch_alias(self) -> str:
        return self.opensearch_alias_for(Phase10ResourceDomain.ROLLOUT)

    def opensearch_alias_for(self, domain: Phase10ResourceDomain) -> str:
        if domain is Phase10ResourceDomain.VALIDATION:
            return f"phase10-m7b-{self.run_token}-validation-current"
        if domain is Phase10ResourceDomain.ROLLOUT:
            return f"phase10-m7b-{self.run_token}-current"
        raise Phase10IntegrationRunContextError("Phase 10 resource domain is invalid.")

    @property
    def document_prefix(self) -> str:
        return self.document_prefix_for(Phase10ResourceDomain.ROLLOUT)

    def document_prefix_for(self, domain: Phase10ResourceDomain) -> str:
        if domain is Phase10ResourceDomain.VALIDATION:
            return f"phase10-hard-delete-{self.run_token}-validation-"
        if domain is Phase10ResourceDomain.ROLLOUT:
            return f"phase10-hard-delete-{self.run_token}-"
        raise Phase10IntegrationRunContextError("Phase 10 resource domain is invalid.")

    def build_document_namespace(
        self,
        document_id: UUID,
        *,
        resource_domain: Phase10ResourceDomain,
        document_type: str = "core",
    ) -> str:
        if document_type not in _DOCUMENT_TYPES:
            raise Phase10IntegrationRunContextError(
                "Phase 10 integration document type is not allowed."
            )
        if resource_domain is Phase10ResourceDomain.VALIDATION and document_type != "core":
            raise Phase10IntegrationRunContextError(
                "Phase 10 validation documents do not support rollout subtypes."
            )
        subtype = "" if document_type == "core" else f"{document_type}-"
        return f"{self.document_prefix_for(resource_domain)}{subtype}{document_id}"

    def owns_document_namespace(
        self,
        namespace: str,
        document_id: UUID,
        *,
        resource_domain: Phase10ResourceDomain,
    ) -> bool:
        document_types = (
            {"core"}
            if resource_domain is Phase10ResourceDomain.VALIDATION
            else _DOCUMENT_TYPES
        )
        return any(
            namespace
            == self.build_document_namespace(
                document_id,
                resource_domain=resource_domain,
                document_type=document_type,
            )
            for document_type in document_types
        )

    def validate_configured_resources(
        self,
        *,
        validation_postgres_database: str,
        rollout_postgres_database: str,
        migration_database: str,
        restore_database: str,
        validation_minio_bucket: str,
        rollout_minio_bucket: str,
        validation_opensearch_index: str,
        rollout_opensearch_index: str,
        validation_opensearch_alias: str,
        rollout_opensearch_alias: str,
    ) -> None:
        configured = {
            "validation PostgreSQL database": validation_postgres_database,
            "rollout PostgreSQL database": rollout_postgres_database,
            "migration database": migration_database,
            "restore database": restore_database,
            "validation MinIO bucket": validation_minio_bucket,
            "rollout MinIO bucket": rollout_minio_bucket,
            "validation OpenSearch index": validation_opensearch_index,
            "rollout OpenSearch index": rollout_opensearch_index,
            "validation OpenSearch alias": validation_opensearch_alias,
            "rollout OpenSearch alias": rollout_opensearch_alias,
        }
        expected = {
            "validation PostgreSQL database": self.postgres_database_for(
                Phase10ResourceDomain.VALIDATION
            ),
            "rollout PostgreSQL database": self.postgres_database_for(
                Phase10ResourceDomain.ROLLOUT
            ),
            "migration database": self.migration_database,
            "restore database": self.restore_database,
            "validation MinIO bucket": self.minio_bucket_for(
                Phase10ResourceDomain.VALIDATION
            ),
            "rollout MinIO bucket": self.minio_bucket_for(Phase10ResourceDomain.ROLLOUT),
            "validation OpenSearch index": self.opensearch_index_for(
                Phase10ResourceDomain.VALIDATION
            ),
            "rollout OpenSearch index": self.opensearch_index_for(
                Phase10ResourceDomain.ROLLOUT
            ),
            "validation OpenSearch alias": self.opensearch_alias_for(
                Phase10ResourceDomain.VALIDATION
            ),
            "rollout OpenSearch alias": self.opensearch_alias_for(
                Phase10ResourceDomain.ROLLOUT
            ),
        }
        for field, actual in configured.items():
            if actual != expected[field]:
                raise Phase10IntegrationRunContextError(
                    f"Phase 10 integration run context {field} mismatch."
                )
        database_names = {
            validation_postgres_database,
            rollout_postgres_database,
            migration_database,
            restore_database,
        }
        if len(database_names) != 4:
            raise Phase10IntegrationRunContextError(
                "Phase 10 integration PostgreSQL resource domains must be distinct."
            )
        if validation_minio_bucket == rollout_minio_bucket:
            raise Phase10IntegrationRunContextError(
                "Phase 10 integration MinIO resource domains must be distinct."
            )
        if validation_opensearch_index == rollout_opensearch_index:
            raise Phase10IntegrationRunContextError(
                "Phase 10 integration OpenSearch index domains must be distinct."
            )
        if validation_opensearch_alias == rollout_opensearch_alias:
            raise Phase10IntegrationRunContextError(
                "Phase 10 integration OpenSearch alias domains must be distinct."
            )

    def validate_storage_extension_resources(
        self,
        *,
        validation_minio_locked_bucket: str,
        validation_minio_missing_bucket: str,
        validation_opensearch_rollover_index: str,
    ) -> None:
        configured = {
            "validation locked bucket": validation_minio_locked_bucket,
            "validation missing bucket": validation_minio_missing_bucket,
            "validation rollover index": validation_opensearch_rollover_index,
        }
        expected = {
            "validation locked bucket": self.validation_minio_locked_bucket,
            "validation missing bucket": self.validation_minio_missing_bucket,
            "validation rollover index": self.validation_opensearch_rollover_index,
        }
        for field, actual in configured.items():
            if actual != expected[field]:
                raise Phase10IntegrationRunContextError(
                    f"Phase 10 integration run context {field} mismatch."
                )

    def safe_summary(self) -> dict[str, str]:
        return {
            "run_token": self.run_token,
            "validation_postgres_database": self.postgres_database_for(
                Phase10ResourceDomain.VALIDATION
            ),
            "rollout_postgres_database": self.postgres_database_for(
                Phase10ResourceDomain.ROLLOUT
            ),
            "migration_database": self.migration_database,
            "restore_database": self.restore_database,
            "validation_minio_bucket": self.minio_bucket_for(
                Phase10ResourceDomain.VALIDATION
            ),
            "rollout_minio_bucket": self.minio_bucket_for(Phase10ResourceDomain.ROLLOUT),
            "validation_minio_locked_bucket": self.validation_minio_locked_bucket,
            "validation_minio_missing_bucket": self.validation_minio_missing_bucket,
            "validation_opensearch_index": self.opensearch_index_for(
                Phase10ResourceDomain.VALIDATION
            ),
            "validation_opensearch_rollover_index": (
                self.validation_opensearch_rollover_index
            ),
            "rollout_opensearch_index": self.opensearch_index_for(
                Phase10ResourceDomain.ROLLOUT
            ),
            "validation_opensearch_alias": self.opensearch_alias_for(
                Phase10ResourceDomain.VALIDATION
            ),
            "rollout_opensearch_alias": self.opensearch_alias_for(
                Phase10ResourceDomain.ROLLOUT
            ),
            "validation_document_prefix": self.document_prefix_for(
                Phase10ResourceDomain.VALIDATION
            ),
            "rollout_document_prefix": self.document_prefix_for(
                Phase10ResourceDomain.ROLLOUT
            ),
        }
