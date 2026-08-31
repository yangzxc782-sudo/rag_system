from __future__ import annotations

from uuid import UUID

import pytest

from integration.phase10_support import (
    PHASE10_FIXTURE_PREFIX,
    Phase10IntegrationGateError,
    Phase10ResourceSnapshot,
    Phase10TestDocumentFactory,
    assert_unrelated_resources_unchanged,
    load_phase10_integration_settings,
)


def safe_environment() -> dict[str, str]:
    return {
        "PHASE10_INTEGRATION_ENABLED": "1",
        "PHASE10_M7_ROLLOUT_AUTHORIZED": "1",
        "PHASE10_INTEGRATION_ENVIRONMENT": "dedicated-local-test",
        "PHASE10_INTEGRATION_DATABASE_URL": (
            "postgresql+psycopg://phase10_user:secret@127.0.0.1:55432/phase10_integration"
        ),
        "PHASE10_INTEGRATION_DATABASE_NAME_CONFIRM": "phase10_integration",
        "PHASE10_INTEGRATION_MIGRATION_DATABASE_URL": (
            "postgresql+psycopg://phase10_user:secret@127.0.0.1:55432/phase10_migration"
        ),
        "PHASE10_INTEGRATION_MIGRATION_DATABASE_NAME_CONFIRM": "phase10_migration",
        "PHASE10_INTEGRATION_MINIO_ENDPOINT": "127.0.0.1:59000",
        "PHASE10_INTEGRATION_MINIO_ACCESS_KEY": "phase10-access",
        "PHASE10_INTEGRATION_MINIO_SECRET_KEY": "phase10-secret",
        "PHASE10_INTEGRATION_MINIO_BUCKET": "phase10-hard-delete-tests",
        "PHASE10_INTEGRATION_MINIO_BUCKET_CONFIRM": "phase10-hard-delete-tests",
        "PHASE10_INTEGRATION_MINIO_SECURE": "false",
        "PHASE10_INTEGRATION_OPENSEARCH_URL": "http://127.0.0.1:59200",
        "PHASE10_INTEGRATION_OPENSEARCH_INDEX": "phase10-casting-chunks-v1",
        "PHASE10_INTEGRATION_OPENSEARCH_ALIAS": "phase10-casting-chunks-current",
        "PHASE10_INTEGRATION_OPENSEARCH_TARGET_CONFIRM": (
            "phase10-casting-chunks-v1:phase10-casting-chunks-current"
        ),
    }


def test_integration_gate_is_disabled_without_both_explicit_switches() -> None:
    assert load_phase10_integration_settings({}) is None
    assert (
        load_phase10_integration_settings(
            {
                "PHASE10_INTEGRATION_ENABLED": "1",
                "PHASE10_INTEGRATION_ENVIRONMENT": "dedicated-local-test",
            }
        )
        is None
    )


def test_integration_gate_accepts_only_complete_dedicated_configuration() -> None:
    settings = load_phase10_integration_settings(safe_environment())

    assert settings is not None
    assert settings.database_name == "phase10_integration"
    assert settings.migration_database_name == "phase10_migration"
    assert settings.minio_bucket == "phase10-hard-delete-tests"
    assert settings.opensearch_index == "phase10-casting-chunks-v1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PHASE10_INTEGRATION_ENVIRONMENT", "local"),
        ("PHASE10_INTEGRATION_DATABASE_URL", "postgresql+psycopg://u:p@localhost/rag_system"),
        ("PHASE10_INTEGRATION_DATABASE_NAME_CONFIRM", "wrong_database"),
        ("PHASE10_INTEGRATION_MINIO_BUCKET", "rag-documents"),
        ("PHASE10_INTEGRATION_MINIO_BUCKET_CONFIRM", "wrong-bucket"),
        ("PHASE10_INTEGRATION_OPENSEARCH_TARGET_CONFIRM", "wrong:index"),
    ],
)
def test_integration_gate_rejects_unsafe_or_unconfirmed_targets(
    field: str,
    value: str,
) -> None:
    environment = safe_environment()
    environment[field] = value

    with pytest.raises(Phase10IntegrationGateError):
        load_phase10_integration_settings(environment)


def test_integration_gate_fails_closed_when_enabled_configuration_is_incomplete() -> None:
    environment = safe_environment()
    environment.pop("PHASE10_INTEGRATION_MINIO_SECRET_KEY")

    with pytest.raises(Phase10IntegrationGateError, match="required"):
        load_phase10_integration_settings(environment)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PHASE10_INTEGRATION_OPENSEARCH_INDEX", "casting_chunks_v1"),
        ("PHASE10_INTEGRATION_OPENSEARCH_ALIAS", "casting_chunks_current"),
    ],
)
def test_integration_gate_rejects_non_phase10_opensearch_names(
    field: str,
    value: str,
) -> None:
    environment = safe_environment()
    environment[field] = value
    environment["PHASE10_INTEGRATION_OPENSEARCH_TARGET_CONFIRM"] = (
        f"{environment['PHASE10_INTEGRATION_OPENSEARCH_INDEX']}:"
        f"{environment['PHASE10_INTEGRATION_OPENSEARCH_ALIAS']}"
    )

    with pytest.raises(Phase10IntegrationGateError, match="dedicated phase10"):
        load_phase10_integration_settings(environment)


def test_test_document_factory_generates_owned_unique_resource_identity() -> None:
    assert PHASE10_FIXTURE_PREFIX == "phase10-hard-delete-r3-"
    factory = Phase10TestDocumentFactory()

    first = factory.create()
    second = factory.create()

    assert first.namespace.startswith(PHASE10_FIXTURE_PREFIX)
    assert first.namespace != second.namespace
    assert first.document_id != second.document_id
    assert first.filename == f"{first.namespace}.pdf"
    assert first.raw_object_key.endswith(f"/{first.document_id}.pdf")
    assert first.derived_prefix == f"parsed-assets/{first.document_id}/"
    assert all(isinstance(value, UUID) for value in first.chunk_ids)
    assert first.owns_document_id(first.document_id)
    assert not first.owns_document_id(UUID("00000000-0000-0000-0000-000000000001"))


def test_unrelated_snapshot_comparison_ignores_only_explicit_target_resources() -> None:
    target_document = UUID("10000000-0000-0000-0000-000000000001")
    target_chunk = UUID("10000000-0000-0000-0000-000000000002")
    other_document = UUID("20000000-0000-0000-0000-000000000001")
    other_chunk = UUID("20000000-0000-0000-0000-000000000002")
    before = Phase10ResourceSnapshot(
        postgres_documents=frozenset({target_document, other_document}),
        postgres_chunks=frozenset({target_chunk, other_chunk}),
        postgres_parse_runs=frozenset(),
        postgres_assets=frozenset(),
        postgres_knowledge_items=frozenset(),
        postgres_knowledge_sources=frozenset(),
        minio_versions=frozenset(
            {
                (f"raw/2026/08/{target_document}.pdf", "target-v1", False),
                (f"raw/2026/08/{other_document}.pdf", "other-v1", False),
            }
        ),
        opensearch_ids=frozenset({str(target_chunk), str(other_chunk)}),
        opensearch_document_count=2,
        opensearch_alias_targets=("phase10-casting-chunks-v1",),
        opensearch_mapping_sha256="same-mapping",
    )
    after = Phase10ResourceSnapshot(
        postgres_documents=frozenset({other_document}),
        postgres_chunks=frozenset({other_chunk}),
        postgres_parse_runs=frozenset(),
        postgres_assets=frozenset(),
        postgres_knowledge_items=frozenset(),
        postgres_knowledge_sources=frozenset(),
        minio_versions=frozenset(
            {(f"raw/2026/08/{other_document}.pdf", "other-v1", False)}
        ),
        opensearch_ids=frozenset({str(other_chunk)}),
        opensearch_document_count=1,
        opensearch_alias_targets=("phase10-casting-chunks-v1",),
        opensearch_mapping_sha256="same-mapping",
    )

    assert_unrelated_resources_unchanged(
        before,
        after,
        target_document_ids={target_document},
        target_chunk_ids={target_chunk},
        target_minio_keys={f"raw/2026/08/{target_document}.pdf"},
        target_opensearch_ids={str(target_chunk)},
    )


def test_unrelated_snapshot_comparison_detects_non_target_change() -> None:
    other_document = UUID("20000000-0000-0000-0000-000000000001")
    before = Phase10ResourceSnapshot(postgres_documents=frozenset({other_document}))
    after = Phase10ResourceSnapshot(postgres_documents=frozenset())

    with pytest.raises(AssertionError, match="postgres_documents"):
        assert_unrelated_resources_unchanged(before, after)
