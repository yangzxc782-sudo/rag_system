from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest
from minio import Minio
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.search_engine.client import create_document_deletion_search_engine_client
from app.search_engine.index_schema import build_casting_chunks_index_body
from integration.phase10_run_context import (
    Phase10IntegrationRunContext,
    Phase10ResourceDomain,
)
from integration.phase10_support import (
    Phase10IntegrationGateError,
    Phase10IntegrationSettings,
    Phase10ResourceSettings,
    Phase10TestDocumentFactory,
    assert_phase10_rollout_pristine,
    capture_minio_snapshot,
    capture_opensearch_snapshot,
    capture_postgresql_snapshot,
    load_phase10_integration_settings,
    merge_resource_snapshots,
    stable_json_sha256,
)


@pytest.fixture(scope="session", autouse=True)
def _phase10_integration_gate() -> Phase10IntegrationSettings:
    settings = load_phase10_integration_settings(os.environ)
    if settings is None:
        pytest.skip(
            "Phase 10 integration requires both explicit enablement and rollout authorization."
        )
    return settings


@pytest.fixture(scope="session")
def phase10_integration_settings(
    _phase10_integration_gate: Phase10IntegrationSettings,
) -> Phase10IntegrationSettings:
    return _phase10_integration_gate


@pytest.fixture(scope="session")
def phase10_run_context(
    phase10_integration_settings: Phase10IntegrationSettings,
) -> Phase10IntegrationRunContext:
    return phase10_integration_settings.run_context


@pytest.fixture(scope="session")
def phase10_validation_settings(
    phase10_integration_settings: Phase10IntegrationSettings,
) -> Phase10ResourceSettings:
    return phase10_integration_settings.for_domain(Phase10ResourceDomain.VALIDATION)


@pytest.fixture(scope="session")
def phase10_rollout_settings(
    phase10_integration_settings: Phase10IntegrationSettings,
) -> Phase10ResourceSettings:
    return phase10_integration_settings.for_domain(Phase10ResourceDomain.ROLLOUT)


def _phase10_domain_engine(settings: Phase10ResourceSettings):
    engine = create_engine(settings.database_url, poolclass=NullPool)
    with engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
    if version != "0008_phase10_enforce":
        engine.dispose()
        raise Phase10IntegrationGateError(
            f"Dedicated {settings.resource_domain.value} database is not at the Phase 10 "
            "Alembic head."
        )
    return engine


@pytest.fixture(scope="session")
def phase10_validation_engine(phase10_validation_settings: Phase10ResourceSettings):
    engine = _phase10_domain_engine(phase10_validation_settings)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def phase10_rollout_engine(phase10_rollout_settings: Phase10ResourceSettings):
    engine = _phase10_domain_engine(phase10_rollout_settings)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def phase10_migration_engine(phase10_integration_settings: Phase10IntegrationSettings):
    engine = create_engine(
        phase10_integration_settings.migration_database_url,
        poolclass=NullPool,
    )
    try:
        yield engine
    finally:
        engine.dispose()


def _session_factory(engine):
    return sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )


@pytest.fixture(scope="session")
def phase10_validation_session_factory(phase10_validation_engine):
    return _session_factory(phase10_validation_engine)


@pytest.fixture(scope="session")
def phase10_rollout_session_factory(phase10_rollout_engine):
    return _session_factory(phase10_rollout_engine)


def _phase10_minio_client(
    integration_settings: Phase10IntegrationSettings,
    resource_settings: Phase10ResourceSettings,
) -> Minio:
    client = Minio(
        endpoint=integration_settings.minio_endpoint,
        access_key=integration_settings.minio_access_key,
        secret_key=integration_settings.minio_secret_key,
        secure=integration_settings.minio_secure,
    )
    if not client.bucket_exists(resource_settings.minio_bucket):
        raise Phase10IntegrationGateError(
            f"Dedicated {resource_settings.resource_domain.value} MinIO bucket does not exist."
        )
    versioning = client.get_bucket_versioning(resource_settings.minio_bucket)
    if str(getattr(versioning, "status", "")).lower() != "enabled":
        raise Phase10IntegrationGateError(
            f"Dedicated {resource_settings.resource_domain.value} MinIO bucket must have "
            "versioning enabled."
        )
    return client


@pytest.fixture(scope="session")
def phase10_validation_minio_client(
    phase10_integration_settings: Phase10IntegrationSettings,
    phase10_validation_settings: Phase10ResourceSettings,
) -> Minio:
    return _phase10_minio_client(
        phase10_integration_settings,
        phase10_validation_settings,
    )


@pytest.fixture(scope="session")
def phase10_rollout_minio_client(
    phase10_integration_settings: Phase10IntegrationSettings,
    phase10_rollout_settings: Phase10ResourceSettings,
) -> Minio:
    return _phase10_minio_client(
        phase10_integration_settings,
        phase10_rollout_settings,
    )


def _phase10_runtime_settings(
    integration_settings: Phase10IntegrationSettings,
    resource_settings: Phase10ResourceSettings,
) -> Settings:
    return Settings(
        database_url=resource_settings.database_url,
        minio_endpoint=integration_settings.minio_endpoint,
        minio_root_user=integration_settings.minio_access_key,
        minio_root_password=integration_settings.minio_secret_key,
        minio_bucket=resource_settings.minio_bucket,
        minio_secure=integration_settings.minio_secure,
        search_engine_url=integration_settings.opensearch_url,
        search_engine_username=integration_settings.opensearch_username,
        search_engine_password=integration_settings.opensearch_password,
        search_engine_verify_ssl=integration_settings.opensearch_verify_ssl,
        search_index_name=resource_settings.opensearch_index,
        search_index_alias=resource_settings.opensearch_alias,
        document_deletion_executor_enabled=True,
        document_deletion_storage_timeout_seconds=30,
        document_deletion_lease_seconds=120,
    )


@pytest.fixture(scope="session")
def phase10_validation_runtime_settings(
    phase10_integration_settings: Phase10IntegrationSettings,
    phase10_validation_settings: Phase10ResourceSettings,
) -> Settings:
    return _phase10_runtime_settings(
        phase10_integration_settings,
        phase10_validation_settings,
    )


@pytest.fixture(scope="session")
def phase10_rollout_runtime_settings(
    phase10_integration_settings: Phase10IntegrationSettings,
    phase10_rollout_settings: Phase10ResourceSettings,
) -> Settings:
    return _phase10_runtime_settings(
        phase10_integration_settings,
        phase10_rollout_settings,
    )


def _phase10_opensearch_client(
    integration_settings: Phase10IntegrationSettings,
    resource_settings: Phase10ResourceSettings,
    runtime_settings: Settings,
):
    parsed = urlparse(integration_settings.opensearch_url)
    if parsed.hostname is None:
        raise Phase10IntegrationGateError("Dedicated OpenSearch URL is invalid.")
    client = create_document_deletion_search_engine_client(runtime_settings)
    if not client.indices.exists(index=resource_settings.opensearch_index):
        raise Phase10IntegrationGateError(
            f"Dedicated {resource_settings.resource_domain.value} OpenSearch index does "
            "not exist."
        )
    if not client.indices.exists_alias(name=resource_settings.opensearch_alias):
        raise Phase10IntegrationGateError(
            f"Dedicated {resource_settings.resource_domain.value} OpenSearch alias does "
            "not exist."
        )
    mapping = client.indices.get_mapping(index=resource_settings.opensearch_index)
    properties = mapping[resource_settings.opensearch_index]["mappings"]["properties"]
    if int(properties["embedding"]["dimension"]) != 1024:
        raise Phase10IntegrationGateError(
            "Dedicated Phase 10 OpenSearch vector dimension is not 1024."
        )
    return client


@pytest.fixture(scope="session")
def phase10_validation_opensearch_client(
    phase10_integration_settings: Phase10IntegrationSettings,
    phase10_validation_settings: Phase10ResourceSettings,
    phase10_validation_runtime_settings: Settings,
):
    return _phase10_opensearch_client(
        phase10_integration_settings,
        phase10_validation_settings,
        phase10_validation_runtime_settings,
    )


@pytest.fixture(scope="session")
def phase10_rollout_opensearch_client(
    phase10_integration_settings: Phase10IntegrationSettings,
    phase10_rollout_settings: Phase10ResourceSettings,
    phase10_rollout_runtime_settings: Settings,
):
    return _phase10_opensearch_client(
        phase10_integration_settings,
        phase10_rollout_settings,
        phase10_rollout_runtime_settings,
    )


@pytest.fixture
def phase10_validation_document_factory(
    phase10_run_context: Phase10IntegrationRunContext,
) -> Phase10TestDocumentFactory:
    return Phase10TestDocumentFactory(
        run_context=phase10_run_context,
        resource_domain=Phase10ResourceDomain.VALIDATION,
    )


@pytest.fixture
def phase10_rollout_document_factory(
    phase10_run_context: Phase10IntegrationRunContext,
) -> Phase10TestDocumentFactory:
    return Phase10TestDocumentFactory(
        run_context=phase10_run_context,
        resource_domain=Phase10ResourceDomain.ROLLOUT,
    )


@pytest.fixture(scope="session")
def phase10_rollout_pristine(
    phase10_run_context: Phase10IntegrationRunContext,
    phase10_rollout_settings: Phase10ResourceSettings,
    phase10_rollout_session_factory,
    phase10_rollout_minio_client: Minio,
    phase10_rollout_opensearch_client,
    phase10_rollout_runtime_settings: Settings,
) -> None:
    with phase10_rollout_session_factory() as session:
        postgres = capture_postgresql_snapshot(session)
    minio = capture_minio_snapshot(
        phase10_rollout_minio_client,
        phase10_rollout_settings.minio_bucket,
    )
    opensearch = capture_opensearch_snapshot(
        phase10_rollout_opensearch_client,
        index_name=phase10_rollout_settings.opensearch_index,
        alias_name=phase10_rollout_settings.opensearch_alias,
    )
    expected_mapping = build_casting_chunks_index_body(
        phase10_rollout_runtime_settings
    )["mappings"]
    assert_phase10_rollout_pristine(
        merge_resource_snapshots(postgres, minio, opensearch),
        run_context=phase10_run_context,
        expected_mapping_sha256=stable_json_sha256(expected_mapping),
    )
