from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from minio import Minio
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.search_engine.client import create_document_deletion_search_engine_client
from integration.phase10_support import (
    Phase10IntegrationGateError,
    Phase10IntegrationSettings,
    Phase10TestDocumentFactory,
    load_phase10_integration_settings,
)
from integration.phase10_run_context import Phase10IntegrationRunContext


@pytest.fixture(scope="session", autouse=True)
def _phase10_integration_gate() -> Phase10IntegrationSettings:
    settings = load_phase10_integration_settings(os.environ)
    if settings is None:
        pytest.skip(
            "Phase 10 integration requires both explicit enablement and rollout authorization."
        )
    return settings


@pytest.fixture(scope="session")
def phase10_settings(
    _phase10_integration_gate: Phase10IntegrationSettings,
) -> Phase10IntegrationSettings:
    return _phase10_integration_gate


@pytest.fixture(scope="session")
def phase10_run_context(
    phase10_settings: Phase10IntegrationSettings,
) -> Phase10IntegrationRunContext:
    return phase10_settings.run_context


@pytest.fixture(scope="session")
def phase10_engine(phase10_settings: Phase10IntegrationSettings):
    engine = create_engine(
        phase10_settings.database_url,
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if version != "0008_phase10_enforce":
            raise Phase10IntegrationGateError(
                "Dedicated integration database is not at the Phase 10 Alembic head."
            )
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def phase10_migration_engine(phase10_settings: Phase10IntegrationSettings):
    engine = create_engine(
        phase10_settings.migration_database_url,
        poolclass=NullPool,
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def phase10_session_factory(phase10_engine):
    return sessionmaker(
        bind=phase10_engine,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )


@pytest.fixture(scope="session")
def phase10_minio_client(phase10_settings: Phase10IntegrationSettings) -> Minio:
    client = Minio(
        endpoint=phase10_settings.minio_endpoint,
        access_key=phase10_settings.minio_access_key,
        secret_key=phase10_settings.minio_secret_key,
        secure=phase10_settings.minio_secure,
    )
    if not client.bucket_exists(phase10_settings.minio_bucket):
        raise Phase10IntegrationGateError(
            "Dedicated Phase 10 MinIO bucket does not exist."
        )
    versioning = client.get_bucket_versioning(phase10_settings.minio_bucket)
    if str(getattr(versioning, "status", "")).lower() != "enabled":
        raise Phase10IntegrationGateError(
            "Dedicated Phase 10 MinIO bucket must have versioning enabled."
        )
    return client


@pytest.fixture(scope="session")
def phase10_runtime_settings(
    phase10_settings: Phase10IntegrationSettings,
) -> Settings:
    return Settings(
        database_url=phase10_settings.database_url,
        minio_endpoint=phase10_settings.minio_endpoint,
        minio_root_user=phase10_settings.minio_access_key,
        minio_root_password=phase10_settings.minio_secret_key,
        minio_bucket=phase10_settings.minio_bucket,
        minio_secure=phase10_settings.minio_secure,
        search_engine_url=phase10_settings.opensearch_url,
        search_engine_username=phase10_settings.opensearch_username,
        search_engine_password=phase10_settings.opensearch_password,
        search_engine_verify_ssl=phase10_settings.opensearch_verify_ssl,
        search_index_name=phase10_settings.opensearch_index,
        search_index_alias=phase10_settings.opensearch_alias,
        document_deletion_executor_enabled=True,
        document_deletion_storage_timeout_seconds=30,
        document_deletion_lease_seconds=120,
    )


@pytest.fixture(scope="session")
def phase10_opensearch_client(
    phase10_settings: Phase10IntegrationSettings,
    phase10_runtime_settings: Settings,
):
    parsed = urlparse(phase10_settings.opensearch_url)
    if parsed.hostname is None:
        raise Phase10IntegrationGateError("Dedicated OpenSearch URL is invalid.")
    client = create_document_deletion_search_engine_client(phase10_runtime_settings)
    if not client.indices.exists(index=phase10_settings.opensearch_index):
        raise Phase10IntegrationGateError(
            "Dedicated Phase 10 OpenSearch index does not exist."
        )
    if not client.indices.exists_alias(name=phase10_settings.opensearch_alias):
        raise Phase10IntegrationGateError(
            "Dedicated Phase 10 OpenSearch alias does not exist."
        )
    mapping = client.indices.get_mapping(index=phase10_settings.opensearch_index)
    properties = mapping[phase10_settings.opensearch_index]["mappings"]["properties"]
    if int(properties["embedding"]["dimension"]) != 1024:
        raise Phase10IntegrationGateError(
            "Dedicated Phase 10 OpenSearch vector dimension is not 1024."
        )
    return client


@pytest.fixture
def phase10_document_factory(
    phase10_run_context: Phase10IntegrationRunContext,
) -> Phase10TestDocumentFactory:
    return Phase10TestDocumentFactory(run_context=phase10_run_context)
