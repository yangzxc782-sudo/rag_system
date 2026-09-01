from __future__ import annotations

import re
from dataclasses import replace
from uuid import UUID
from uuid import uuid4

import pytest

from integration import phase10_support
from integration.phase10_run_context import Phase10IntegrationRunContext
from integration.phase10_support import (
    Phase10IntegrationGateError,
    Phase10ResourceSnapshot,
    Phase10TestDocumentFactory,
    assert_unrelated_resources_unchanged,
    load_phase10_integration_settings,
    run_alembic,
)


def safe_environment() -> dict[str, str]:
    return {
        "PHASE10_INTEGRATION_ENABLED": "1",
        "PHASE10_M7_ROLLOUT_AUTHORIZED": "1",
        "PHASE10_INTEGRATION_ENVIRONMENT": "dedicated-local-test",
        "PHASE10_RUN_TOKEN": "abcdef12",
        "PHASE10_INTEGRATION_DATABASE_URL": (
            "postgresql+psycopg://phase10_user:secret@127.0.0.1:55432/"
            "phase10_m7b_abcdef12"
        ),
        "PHASE10_INTEGRATION_DATABASE_NAME_CONFIRM": "phase10_m7b_abcdef12",
        "PHASE10_INTEGRATION_MIGRATION_DATABASE_URL": (
            "postgresql+psycopg://phase10_user:secret@127.0.0.1:55432/"
            "phase10_m7b_migration_abcdef12"
        ),
        "PHASE10_INTEGRATION_MIGRATION_DATABASE_NAME_CONFIRM": (
            "phase10_m7b_migration_abcdef12"
        ),
        "PHASE10_INTEGRATION_RESTORE_DATABASE_URL": (
            "postgresql+psycopg://phase10_user:secret@127.0.0.1:55432/"
            "phase10_m7b_restore_abcdef12"
        ),
        "PHASE10_INTEGRATION_RESTORE_DATABASE_NAME_CONFIRM": (
            "phase10_m7b_restore_abcdef12"
        ),
        "PHASE10_INTEGRATION_MINIO_ENDPOINT": "127.0.0.1:59000",
        "PHASE10_INTEGRATION_MINIO_ACCESS_KEY": "phase10-access",
        "PHASE10_INTEGRATION_MINIO_SECRET_KEY": "phase10-secret",
        "PHASE10_INTEGRATION_MINIO_BUCKET": "phase10-m7b-abcdef12",
        "PHASE10_INTEGRATION_MINIO_BUCKET_CONFIRM": "phase10-m7b-abcdef12",
        "PHASE10_INTEGRATION_MINIO_SECURE": "false",
        "PHASE10_INTEGRATION_OPENSEARCH_URL": "http://127.0.0.1:59200",
        "PHASE10_INTEGRATION_OPENSEARCH_INDEX": "phase10-m7b-abcdef12-v1",
        "PHASE10_INTEGRATION_OPENSEARCH_ALIAS": "phase10-m7b-abcdef12-current",
        "PHASE10_INTEGRATION_OPENSEARCH_TARGET_CONFIRM": (
            "phase10-m7b-abcdef12-v1:phase10-m7b-abcdef12-current"
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
    assert settings.run_context.run_token == "abcdef12"
    assert settings.database_name == "phase10_m7b_abcdef12"
    assert settings.migration_database_name == "phase10_m7b_migration_abcdef12"
    assert settings.restore_database_name == "phase10_m7b_restore_abcdef12"
    assert settings.minio_bucket == "phase10-m7b-abcdef12"
    assert settings.opensearch_index == "phase10-m7b-abcdef12-v1"


def test_integration_gate_requires_run_token_when_rollout_is_enabled() -> None:
    environment = safe_environment()
    environment.pop("PHASE10_RUN_TOKEN")

    with pytest.raises(Phase10IntegrationGateError, match="PHASE10_RUN_TOKEN"):
        load_phase10_integration_settings(environment)


@pytest.mark.parametrize(
    "run_token",
    ["R5TOKEN12", "abc-def1", "../abc123", " abcdef12 "],
)
def test_integration_gate_rejects_invalid_run_token(run_token: str) -> None:
    environment = safe_environment()
    environment["PHASE10_RUN_TOKEN"] = run_token

    with pytest.raises(Phase10IntegrationGateError, match="run token"):
        load_phase10_integration_settings(environment)


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "PHASE10_INTEGRATION_DATABASE_URL": (
                "postgresql+psycopg://u:p@localhost/phase10_m7b_deadbeef"
            ),
            "PHASE10_INTEGRATION_DATABASE_NAME_CONFIRM": "phase10_m7b_deadbeef",
        },
        {
            "PHASE10_INTEGRATION_MIGRATION_DATABASE_URL": (
                "postgresql+psycopg://u:p@localhost/phase10_m7b_migration_deadbeef"
            ),
            "PHASE10_INTEGRATION_MIGRATION_DATABASE_NAME_CONFIRM": (
                "phase10_m7b_migration_deadbeef"
            ),
        },
        {
            "PHASE10_INTEGRATION_RESTORE_DATABASE_URL": (
                "postgresql+psycopg://u:p@localhost/phase10_m7b_restore_deadbeef"
            ),
            "PHASE10_INTEGRATION_RESTORE_DATABASE_NAME_CONFIRM": (
                "phase10_m7b_restore_deadbeef"
            ),
        },
        {
            "PHASE10_INTEGRATION_MINIO_BUCKET": "phase10-m7b-deadbeef",
            "PHASE10_INTEGRATION_MINIO_BUCKET_CONFIRM": "phase10-m7b-deadbeef",
        },
        {
            "PHASE10_INTEGRATION_OPENSEARCH_INDEX": "phase10-m7b-deadbeef-v1",
            "PHASE10_INTEGRATION_OPENSEARCH_TARGET_CONFIRM": (
                "phase10-m7b-deadbeef-v1:phase10-m7b-abcdef12-current"
            ),
        },
        {
            "PHASE10_INTEGRATION_OPENSEARCH_ALIAS": "phase10-m7b-deadbeef-current",
            "PHASE10_INTEGRATION_OPENSEARCH_TARGET_CONFIRM": (
                "phase10-m7b-abcdef12-v1:phase10-m7b-deadbeef-current"
            ),
        },
    ],
)
def test_integration_gate_rejects_cross_run_resource_mismatch(
    overrides: dict[str, str],
) -> None:
    environment = safe_environment()
    environment.update(overrides)

    with pytest.raises(Phase10IntegrationGateError, match="run context"):
        load_phase10_integration_settings(environment)


def test_integration_gate_requires_three_distinct_postgresql_databases() -> None:
    environment = safe_environment()
    environment["PHASE10_INTEGRATION_RESTORE_DATABASE_URL"] = environment[
        "PHASE10_INTEGRATION_DATABASE_URL"
    ]
    environment["PHASE10_INTEGRATION_RESTORE_DATABASE_NAME_CONFIRM"] = environment[
        "PHASE10_INTEGRATION_DATABASE_NAME_CONFIRM"
    ]

    with pytest.raises(Phase10IntegrationGateError, match="must be different"):
        load_phase10_integration_settings(environment)


def test_partial_minio_fault_delegates_real_prefix_then_fails_once() -> None:
    from minio.deleteobjects import DeleteObject
    from integration.phase10_storage_faults import OneShotPartialDeleteMinioClient

    class RecordingMinio:
        def __init__(self) -> None:
            self.deleted: list[tuple[str | None, str | None]] = []

        def remove_objects(self, bucket_name, delete_objects):
            assert bucket_name == "phase10-m7b-abcdef12"
            current = tuple(delete_objects)
            self.deleted.extend((item.name, item.version_id) for item in current)
            return iter(())

    delegate = RecordingMinio()
    client = OneShotPartialDeleteMinioClient(delegate, successful_prefix_size=1)
    objects = (
        DeleteObject("a", "v1"),
        DeleteObject("b", "v2"),
        DeleteObject("c", "v3"),
    )

    first_errors = tuple(client.remove_objects("phase10-m7b-abcdef12", objects))
    second_errors = tuple(client.remove_objects("phase10-m7b-abcdef12", objects[1:]))

    assert delegate.deleted == [("a", "v1"), ("b", "v2"), ("c", "v3")]
    assert [(error.code, error.name, error.version_id) for error in first_errors] == [
        ("InternalError", "b", "v2")
    ]
    assert second_errors == ()
    assert client.first_attempt_deleted == (("a", "v1"),)
    assert client.retry_attempted == (("b", "v2"), ("c", "v3"))


def test_alembic_helper_uses_only_validated_context_migration_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_phase10_integration_settings(safe_environment())
    assert settings is not None
    captured: dict[str, object] = {}

    def fake_run(command, *, cwd, env, check, capture_output, text):
        captured.update(
            command=command,
            cwd=cwd,
            env=env,
            check=check,
            capture_output=capture_output,
            text=text,
        )
        return object()

    monkeypatch.setattr(phase10_support.subprocess, "run", fake_run)

    result = run_alembic(settings, "current")

    assert result is not None
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["DATABASE_URL"] == settings.migration_database_url
    assert environment["DOCUMENT_DELETION_EXECUTOR_ENABLED"] == "false"


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
    context = Phase10IntegrationRunContext(run_token="abcdef12")
    factory = Phase10TestDocumentFactory(run_context=context)

    first = factory.create()
    second = factory.create()

    assert first.namespace.startswith("phase10-hard-delete-abcdef12-")
    assert not re.match(r"phase10-hard-delete-r[0-9]+-", first.namespace)
    assert first.namespace != second.namespace
    assert first.document_id != second.document_id
    assert first.filename == f"{first.namespace}.pdf"
    assert first.raw_object_key.endswith(f"/{first.document_id}.pdf")
    assert first.derived_prefix == f"parsed-assets/{first.document_id}/"
    assert all(isinstance(value, UUID) for value in first.chunk_ids)
    assert first.owns_document_id(first.document_id)
    assert not first.owns_document_id(UUID("00000000-0000-0000-0000-000000000001"))


@pytest.mark.parametrize(
    ("document_type", "expected_prefix"),
    [
        ("core", "phase10-hard-delete-abcdef12-"),
        ("recovery", "phase10-hard-delete-abcdef12-recovery-"),
        ("other", "phase10-hard-delete-abcdef12-other-"),
        ("frontend", "phase10-hard-delete-abcdef12-frontend-"),
    ],
)
def test_factory_derives_each_document_subtype_from_run_context(
    document_type: str,
    expected_prefix: str,
) -> None:
    factory = Phase10TestDocumentFactory(
        run_context=Phase10IntegrationRunContext(run_token="abcdef12")
    )

    identity = factory.create(document_type=document_type)

    assert identity.namespace.startswith(expected_prefix)


def test_factory_registers_only_document_ids_it_created() -> None:
    factory = Phase10TestDocumentFactory(
        run_context=Phase10IntegrationRunContext(run_token="abcdef12")
    )
    first = factory.create()
    second = factory.create(document_type="other")

    assert factory.created_document_ids == frozenset(
        {first.document_id, second.document_id}
    )
    factory.assert_owned_document_id(first.document_id)
    factory.assert_owned_document_id(second.document_id)

    with pytest.raises(Phase10IntegrationGateError, match="not created"):
        factory.assert_owned_document_id(uuid4())


def test_factory_rejects_spoofed_namespace_without_registry_ownership() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")
    factory = Phase10TestDocumentFactory(run_context=context)
    owned = factory.create()
    external_id = uuid4()
    spoofed = replace(
        owned,
        document_id=external_id,
        namespace=context.build_document_namespace(external_id),
    )

    with pytest.raises(Phase10IntegrationGateError, match="not created"):
        factory.assert_owned_document(spoofed)


def test_factory_rejects_owned_id_with_wrong_run_namespace() -> None:
    current = Phase10IntegrationRunContext(run_token="aaaabbbb")
    other = Phase10IntegrationRunContext(run_token="ccccdddd")
    factory = Phase10TestDocumentFactory(run_context=current)
    owned = factory.create()
    wrong_namespace = replace(
        owned,
        namespace=other.build_document_namespace(owned.document_id),
    )

    with pytest.raises(Phase10IntegrationGateError, match="namespace"):
        factory.assert_owned_document(wrong_namespace)


def test_factory_registry_is_isolated_between_run_contexts() -> None:
    factory_a = Phase10TestDocumentFactory(
        run_context=Phase10IntegrationRunContext(run_token="aaaabbbb")
    )
    factory_b = Phase10TestDocumentFactory(
        run_context=Phase10IntegrationRunContext(run_token="ccccdddd")
    )
    document_a = factory_a.create()

    factory_a.assert_owned_document(document_a)
    with pytest.raises(Phase10IntegrationGateError, match="not created"):
        factory_b.assert_owned_document(document_a)
    assert factory_a.run_context.document_prefix != factory_b.run_context.document_prefix


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
