from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_asset import DocumentAsset
from app.models.document_block import DocumentBlock
from app.models.document_chunk import DocumentChunk
from app.models.document_chunk_block import DocumentChunkBlock
from app.models.document_deletion_job import DocumentDeletionJob
from app.models.document_parse_run import DocumentParseRun
from app.models.knowledge_item import KnowledgeItem
from app.models.knowledge_item_chunk import KnowledgeItemChunk
from app.models.knowledge_item_review import KnowledgeItemReview
from app.models.knowledge_item_source import KnowledgeItemSource
from app.models.knowledge_item_version import KnowledgeItemVersion
from app.services.object_storage import list_minio_object_versions
from integration.phase10_run_context import (
    Phase10IntegrationRunContext,
    Phase10IntegrationRunContextError,
)


PHASE10_INTEGRATION_ENVIRONMENT = "dedicated-local-test"


class Phase10IntegrationGateError(RuntimeError):
    """Raised before any client is created when an integration target is unsafe."""


@dataclass(frozen=True, slots=True)
class Phase10IntegrationSettings:
    run_context: Phase10IntegrationRunContext
    database_url: str = field(repr=False)
    database_name: str
    migration_database_url: str = field(repr=False)
    migration_database_name: str
    restore_database_url: str = field(repr=False)
    restore_database_name: str
    minio_endpoint: str
    minio_access_key: str = field(repr=False)
    minio_secret_key: str = field(repr=False)
    minio_bucket: str
    minio_secure: bool
    opensearch_url: str
    opensearch_index: str
    opensearch_alias: str
    opensearch_username: str = field(default="", repr=False)
    opensearch_password: str = field(default="", repr=False)
    opensearch_verify_ssl: bool = False


def load_phase10_integration_settings(
    environ: Mapping[str, str],
) -> Phase10IntegrationSettings | None:
    """Return a fully confirmed dedicated target, or None while rollout is gated."""

    if environ.get("PHASE10_INTEGRATION_ENABLED") != "1":
        return None
    if environ.get("PHASE10_M7_ROLLOUT_AUTHORIZED") != "1":
        return None
    if environ.get("PHASE10_INTEGRATION_ENVIRONMENT") != PHASE10_INTEGRATION_ENVIRONMENT:
        raise Phase10IntegrationGateError(
            "Phase 10 integration environment is not the dedicated local test environment."
        )

    if "PHASE10_RUN_TOKEN" not in environ or environ["PHASE10_RUN_TOKEN"] == "":
        raise Phase10IntegrationGateError(
            "PHASE10_RUN_TOKEN is required for Phase 10 integration."
        )
    run_token = str(environ["PHASE10_RUN_TOKEN"])
    try:
        run_context = Phase10IntegrationRunContext(run_token=run_token)
    except Phase10IntegrationRunContextError as exc:
        raise Phase10IntegrationGateError(str(exc)) from exc

    required = {
        name: _required(environ, name)
        for name in (
            "PHASE10_INTEGRATION_DATABASE_URL",
            "PHASE10_INTEGRATION_DATABASE_NAME_CONFIRM",
            "PHASE10_INTEGRATION_MIGRATION_DATABASE_URL",
            "PHASE10_INTEGRATION_MIGRATION_DATABASE_NAME_CONFIRM",
            "PHASE10_INTEGRATION_RESTORE_DATABASE_URL",
            "PHASE10_INTEGRATION_RESTORE_DATABASE_NAME_CONFIRM",
            "PHASE10_INTEGRATION_MINIO_ENDPOINT",
            "PHASE10_INTEGRATION_MINIO_ACCESS_KEY",
            "PHASE10_INTEGRATION_MINIO_SECRET_KEY",
            "PHASE10_INTEGRATION_MINIO_BUCKET",
            "PHASE10_INTEGRATION_MINIO_BUCKET_CONFIRM",
            "PHASE10_INTEGRATION_OPENSEARCH_URL",
            "PHASE10_INTEGRATION_OPENSEARCH_INDEX",
            "PHASE10_INTEGRATION_OPENSEARCH_ALIAS",
            "PHASE10_INTEGRATION_OPENSEARCH_TARGET_CONFIRM",
        )
    }

    database_name = _validate_database_target(
        required["PHASE10_INTEGRATION_DATABASE_URL"],
        required["PHASE10_INTEGRATION_DATABASE_NAME_CONFIRM"],
        field="integration database",
    )
    migration_database_name = _validate_database_target(
        required["PHASE10_INTEGRATION_MIGRATION_DATABASE_URL"],
        required["PHASE10_INTEGRATION_MIGRATION_DATABASE_NAME_CONFIRM"],
        field="migration database",
    )
    restore_database_name = _validate_database_target(
        required["PHASE10_INTEGRATION_RESTORE_DATABASE_URL"],
        required["PHASE10_INTEGRATION_RESTORE_DATABASE_NAME_CONFIRM"],
        field="restore database",
    )
    if len({database_name, migration_database_name, restore_database_name}) != 3:
        raise Phase10IntegrationGateError(
            "Phase 10 integration, migration, and restore databases must be different."
        )

    bucket = required["PHASE10_INTEGRATION_MINIO_BUCKET"]
    if not bucket.startswith("phase10-"):
        raise Phase10IntegrationGateError(
            "Phase 10 MinIO bucket must be a dedicated phase10-* test bucket."
        )
    if required["PHASE10_INTEGRATION_MINIO_BUCKET_CONFIRM"] != bucket:
        raise Phase10IntegrationGateError("Phase 10 MinIO bucket confirmation does not match.")

    index_name = _validate_opensearch_name(
        required["PHASE10_INTEGRATION_OPENSEARCH_INDEX"],
        field="index",
    )
    alias_name = _validate_opensearch_name(
        required["PHASE10_INTEGRATION_OPENSEARCH_ALIAS"],
        field="alias",
    )
    if required["PHASE10_INTEGRATION_OPENSEARCH_TARGET_CONFIRM"] != f"{index_name}:{alias_name}":
        raise Phase10IntegrationGateError(
            "Phase 10 OpenSearch target confirmation does not match the index and alias."
        )

    opensearch_url = required["PHASE10_INTEGRATION_OPENSEARCH_URL"]
    parsed_search_url = urlparse(opensearch_url)
    if parsed_search_url.scheme not in {"http", "https"} or not parsed_search_url.hostname:
        raise Phase10IntegrationGateError("Phase 10 OpenSearch URL must be an explicit HTTP(S) URL.")

    try:
        run_context.validate_configured_resources(
            postgres_database=database_name,
            migration_database=migration_database_name,
            restore_database=restore_database_name,
            minio_bucket=bucket,
            opensearch_index=index_name,
            opensearch_alias=alias_name,
        )
    except Phase10IntegrationRunContextError as exc:
        raise Phase10IntegrationGateError(str(exc)) from exc

    return Phase10IntegrationSettings(
        run_context=run_context,
        database_url=required["PHASE10_INTEGRATION_DATABASE_URL"],
        database_name=database_name,
        migration_database_url=required["PHASE10_INTEGRATION_MIGRATION_DATABASE_URL"],
        migration_database_name=migration_database_name,
        restore_database_url=required["PHASE10_INTEGRATION_RESTORE_DATABASE_URL"],
        restore_database_name=restore_database_name,
        minio_endpoint=required["PHASE10_INTEGRATION_MINIO_ENDPOINT"],
        minio_access_key=required["PHASE10_INTEGRATION_MINIO_ACCESS_KEY"],
        minio_secret_key=required["PHASE10_INTEGRATION_MINIO_SECRET_KEY"],
        minio_bucket=bucket,
        minio_secure=_bool_value(environ, "PHASE10_INTEGRATION_MINIO_SECURE", default=False),
        opensearch_url=opensearch_url,
        opensearch_index=index_name,
        opensearch_alias=alias_name,
        opensearch_username=str(environ.get("PHASE10_INTEGRATION_OPENSEARCH_USERNAME", "")),
        opensearch_password=str(environ.get("PHASE10_INTEGRATION_OPENSEARCH_PASSWORD", "")),
        opensearch_verify_ssl=_bool_value(
            environ,
            "PHASE10_INTEGRATION_OPENSEARCH_VERIFY_SSL",
            default=False,
        ),
    )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = str(environ.get(name, "")).strip()
    if not value:
        raise Phase10IntegrationGateError(f"{name} is required for Phase 10 integration.")
    return value


def _validate_database_target(url: str, confirmation: str, *, field: str) -> str:
    try:
        parsed = make_url(url)
    except Exception as exc:
        raise Phase10IntegrationGateError(f"Phase 10 {field} URL is invalid.") from exc
    database_name = str(parsed.database or "")
    if not parsed.drivername.startswith("postgresql+psycopg") or not database_name:
        raise Phase10IntegrationGateError(
            f"Phase 10 {field} must use an explicit postgresql+psycopg database URL."
        )
    if not database_name.startswith("phase10_") or database_name == "rag_system":
        raise Phase10IntegrationGateError(
            f"Phase 10 {field} must name a dedicated phase10_* database."
        )
    if confirmation != database_name:
        raise Phase10IntegrationGateError(f"Phase 10 {field} confirmation does not match.")
    return database_name


def _validate_opensearch_name(value: str, *, field: str) -> str:
    if any(character in value for character in "*?,# \\/") or value in {"_all", "all"}:
        raise Phase10IntegrationGateError(f"Phase 10 OpenSearch {field} is unsafe.")
    if not value.startswith("phase10-"):
        raise Phase10IntegrationGateError(
            f"Phase 10 OpenSearch {field} must be a dedicated phase10-* target."
        )
    return value


def _bool_value(environ: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    normalized = str(raw).strip().lower()
    if normalized not in {"true", "false"}:
        raise Phase10IntegrationGateError(f"{name} must be true or false.")
    return normalized == "true"


@dataclass(frozen=True, slots=True)
class Phase10TestDocument:
    namespace: str
    document_id: UUID
    filename: str
    raw_object_key: str
    derived_prefix: str
    parse_run_id: UUID
    block_ids: tuple[UUID, ...]
    asset_ids: tuple[UUID, ...]
    chunk_ids: tuple[UUID, ...]
    chunk_block_ids: tuple[UUID, ...]

    def owns_document_id(self, document_id: UUID) -> bool:
        return document_id == self.document_id

    @property
    def explicit_derived_keys(self) -> tuple[str, ...]:
        return (
            f"{self.derived_prefix}{self.parse_run_id}/output.md",
            f"{self.derived_prefix}{self.parse_run_id}/output.json",
            f"{self.derived_prefix}{self.parse_run_id}/images/page-1.png",
            f"{self.derived_prefix}{self.parse_run_id}/intermediate/source.json",
        )

    @property
    def all_minio_keys(self) -> frozenset[str]:
        return frozenset((self.raw_object_key, *self.explicit_derived_keys))


class Phase10TestDocumentFactory:
    """Generate owned fixture identities; never accepts an existing Document ID."""

    def __init__(
        self,
        *,
        run_context: Phase10IntegrationRunContext,
        now: datetime | None = None,
        chunk_count: int = 2,
    ) -> None:
        self._run_context = run_context
        self._now = now or datetime.now(UTC)
        self._chunk_count = chunk_count
        self._created_document_ids: set[UUID] = set()

    @property
    def run_context(self) -> Phase10IntegrationRunContext:
        return self._run_context

    @property
    def created_document_ids(self) -> frozenset[UUID]:
        return frozenset(self._created_document_ids)

    def assert_owned_document_id(self, document_id: UUID) -> None:
        if document_id not in self._created_document_ids:
            raise Phase10IntegrationGateError(
                "Phase 10 destructive target was not created by this factory."
            )

    def assert_owned_document(self, identity: Phase10TestDocument) -> None:
        self.assert_owned_document_id(identity.document_id)
        if not self._run_context.owns_document_namespace(
            identity.namespace,
            identity.document_id,
        ):
            raise Phase10IntegrationGateError(
                "Phase 10 destructive target namespace does not match the run context."
            )

    def create(self, *, document_type: str = "core") -> Phase10TestDocument:
        document_id = uuid4()
        namespace = self._run_context.build_document_namespace(
            document_id,
            document_type=document_type,
        )
        parse_run_id = uuid4()
        identity = Phase10TestDocument(
            namespace=namespace,
            document_id=document_id,
            filename=f"{namespace}.pdf",
            raw_object_key=f"raw/{self._now:%Y/%m}/{document_id}.pdf",
            derived_prefix=f"parsed-assets/{document_id}/",
            parse_run_id=parse_run_id,
            block_ids=(uuid4(),),
            asset_ids=(uuid4(),),
            chunk_ids=tuple(uuid4() for _ in range(self._chunk_count)),
            chunk_block_ids=tuple(uuid4() for _ in range(self._chunk_count)),
        )
        self._created_document_ids.add(identity.document_id)
        return identity


@dataclass(frozen=True, slots=True)
class Phase10ResourceSnapshot:
    postgres_documents: frozenset[UUID] = frozenset()
    postgres_chunks: frozenset[UUID] = frozenset()
    postgres_parse_runs: frozenset[UUID] = frozenset()
    postgres_assets: frozenset[UUID] = frozenset()
    postgres_blocks: frozenset[UUID] = frozenset()
    postgres_chunk_blocks: frozenset[UUID] = frozenset()
    postgres_knowledge_items: frozenset[UUID] = frozenset()
    postgres_knowledge_sources: frozenset[UUID] = frozenset()
    postgres_knowledge_chunks: frozenset[UUID] = frozenset()
    postgres_knowledge_versions: frozenset[UUID] = frozenset()
    postgres_knowledge_reviews: frozenset[UUID] = frozenset()
    postgres_deletion_jobs: frozenset[UUID] = frozenset()
    minio_versions: frozenset[tuple[str, str | None, bool]] = frozenset()
    opensearch_ids: frozenset[str] = frozenset()
    opensearch_document_count: int = 0
    opensearch_alias_targets: tuple[str, ...] = ()
    opensearch_mapping_sha256: str = ""


def assert_unrelated_resources_unchanged(
    before: Phase10ResourceSnapshot,
    after: Phase10ResourceSnapshot,
    *,
    target_document_ids: set[UUID] | None = None,
    target_chunk_ids: set[UUID] | None = None,
    target_parse_run_ids: set[UUID] | None = None,
    target_asset_ids: set[UUID] | None = None,
    target_block_ids: set[UUID] | None = None,
    target_chunk_block_ids: set[UUID] | None = None,
    target_knowledge_item_ids: set[UUID] | None = None,
    target_knowledge_source_ids: set[UUID] | None = None,
    target_knowledge_chunk_ids: set[UUID] | None = None,
    target_knowledge_version_ids: set[UUID] | None = None,
    target_knowledge_review_ids: set[UUID] | None = None,
    target_deletion_job_ids: set[UUID] | None = None,
    target_minio_keys: set[str] | None = None,
    target_opensearch_ids: set[str] | None = None,
) -> None:
    target_sets: dict[str, set[object]] = {
        "postgres_documents": set(target_document_ids or ()),
        "postgres_chunks": set(target_chunk_ids or ()),
        "postgres_parse_runs": set(target_parse_run_ids or ()),
        "postgres_assets": set(target_asset_ids or ()),
        "postgres_blocks": set(target_block_ids or ()),
        "postgres_chunk_blocks": set(target_chunk_block_ids or ()),
        "postgres_knowledge_items": set(target_knowledge_item_ids or ()),
        "postgres_knowledge_sources": set(target_knowledge_source_ids or ()),
        "postgres_knowledge_chunks": set(target_knowledge_chunk_ids or ()),
        "postgres_knowledge_versions": set(target_knowledge_version_ids or ()),
        "postgres_knowledge_reviews": set(target_knowledge_review_ids or ()),
        "postgres_deletion_jobs": set(target_deletion_job_ids or ()),
    }
    for field_name, targets in target_sets.items():
        before_values = set(getattr(before, field_name)) - targets
        after_values = set(getattr(after, field_name)) - targets
        assert before_values == after_values, f"unrelated {field_name} changed"

    target_keys = target_minio_keys or set()
    before_minio = {entry for entry in before.minio_versions if entry[0] not in target_keys}
    after_minio = {entry for entry in after.minio_versions if entry[0] not in target_keys}
    assert before_minio == after_minio, "unrelated minio_versions changed"

    target_search_ids = target_opensearch_ids or set()
    before_search = set(before.opensearch_ids) - target_search_ids
    after_search = set(after.opensearch_ids) - target_search_ids
    assert before_search == after_search, "unrelated opensearch_ids changed"
    assert (
        before.opensearch_document_count - len(set(before.opensearch_ids) & target_search_ids)
        == after.opensearch_document_count - len(set(after.opensearch_ids) & target_search_ids)
    ), "unrelated OpenSearch document count changed"
    assert before.opensearch_alias_targets == after.opensearch_alias_targets, (
        "OpenSearch alias targets changed"
    )
    assert before.opensearch_mapping_sha256 == after.opensearch_mapping_sha256, (
        "OpenSearch mapping changed"
    )


def stable_json_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_postgresql_snapshot(session: Session) -> Phase10ResourceSnapshot:
    """Read identifier sets only; never loads document or knowledge bodies."""

    def identifiers(model: type[object]) -> frozenset[UUID]:
        return frozenset(session.scalars(select(model.id)).all())

    return Phase10ResourceSnapshot(
        postgres_documents=identifiers(Document),
        postgres_chunks=identifiers(DocumentChunk),
        postgres_parse_runs=identifiers(DocumentParseRun),
        postgres_assets=identifiers(DocumentAsset),
        postgres_blocks=identifiers(DocumentBlock),
        postgres_chunk_blocks=identifiers(DocumentChunkBlock),
        postgres_knowledge_items=identifiers(KnowledgeItem),
        postgres_knowledge_sources=identifiers(KnowledgeItemSource),
        postgres_knowledge_chunks=identifiers(KnowledgeItemChunk),
        postgres_knowledge_versions=identifiers(KnowledgeItemVersion),
        postgres_knowledge_reviews=identifiers(KnowledgeItemReview),
        postgres_deletion_jobs=identifiers(DocumentDeletionJob),
    )


def capture_minio_snapshot(client: object, bucket_name: str) -> Phase10ResourceSnapshot:
    versions = frozenset(
        (entry.object_key, entry.version_id, entry.is_delete_marker)
        for entry in list_minio_object_versions(
            client=client,
            bucket_name=bucket_name,
            prefix="",
        )
    )
    return Phase10ResourceSnapshot(minio_versions=versions)


def capture_opensearch_snapshot(
    client: object,
    *,
    index_name: str,
    alias_name: str,
) -> Phase10ResourceSnapshot:
    alias_response = client.indices.get_alias(name=alias_name)
    alias_targets = tuple(sorted(str(name) for name in alias_response))
    mapping = client.indices.get_mapping(index=index_name)
    count = int(client.count(index=index_name).get("count", 0))
    response = client.search(
        index=index_name,
        body={"query": {"match_all": {}}, "_source": False},
        size=max(count, 1),
        sort=["_id"],
    )
    search_ids = frozenset(
        str(hit["_id"])
        for hit in response.get("hits", {}).get("hits", ())
    )
    if len(search_ids) != count:
        raise AssertionError(
            "Dedicated Phase 10 OpenSearch snapshot exceeded one bounded search page."
        )
    return Phase10ResourceSnapshot(
        opensearch_ids=search_ids,
        opensearch_document_count=count,
        opensearch_alias_targets=alias_targets,
        opensearch_mapping_sha256=stable_json_sha256(mapping),
    )


def merge_resource_snapshots(
    postgres: Phase10ResourceSnapshot,
    minio: Phase10ResourceSnapshot,
    opensearch: Phase10ResourceSnapshot,
) -> Phase10ResourceSnapshot:
    return Phase10ResourceSnapshot(
        postgres_documents=postgres.postgres_documents,
        postgres_chunks=postgres.postgres_chunks,
        postgres_parse_runs=postgres.postgres_parse_runs,
        postgres_assets=postgres.postgres_assets,
        postgres_blocks=postgres.postgres_blocks,
        postgres_chunk_blocks=postgres.postgres_chunk_blocks,
        postgres_knowledge_items=postgres.postgres_knowledge_items,
        postgres_knowledge_sources=postgres.postgres_knowledge_sources,
        postgres_knowledge_chunks=postgres.postgres_knowledge_chunks,
        postgres_knowledge_versions=postgres.postgres_knowledge_versions,
        postgres_knowledge_reviews=postgres.postgres_knowledge_reviews,
        postgres_deletion_jobs=postgres.postgres_deletion_jobs,
        minio_versions=minio.minio_versions,
        opensearch_ids=opensearch.opensearch_ids,
        opensearch_document_count=opensearch.opensearch_document_count,
        opensearch_alias_targets=opensearch.opensearch_alias_targets,
        opensearch_mapping_sha256=opensearch.opensearch_mapping_sha256,
    )


def current_alembic_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        if "alembic_version" not in inspect(connection).get_table_names():
            return None
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def require_clean_migration_database(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    unexpected = tables - {"alembic_version"}
    if unexpected:
        raise AssertionError(
            "Dedicated migration database is not empty; refusing migration test."
        )


def run_alembic(
    settings: Phase10IntegrationSettings,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    """Run Alembic only against an already gated explicit dedicated URL."""

    if settings.migration_database_name != settings.run_context.migration_database:
        raise Phase10IntegrationGateError(
            "Phase 10 migration database does not match the run context."
        )
    backend_dir = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["DATABASE_URL"] = settings.migration_database_url
    environment["DOCUMENT_DELETION_EXECUTOR_ENABLED"] = "false"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=backend_dir,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
