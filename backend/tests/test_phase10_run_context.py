from __future__ import annotations

import pytest

from integration.phase10_run_context import (
    Phase10IntegrationRunContext,
    Phase10IntegrationRunContextError,
    Phase10ResourceDomain,
)


def test_run_context_derives_every_resource_name_from_one_token() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")

    assert context.postgres_database == "phase10_m7b_abcdef12"
    assert context.migration_database == "phase10_m7b_migration_abcdef12"
    assert context.restore_database == "phase10_m7b_restore_abcdef12"
    assert context.minio_bucket == "phase10-m7b-abcdef12"
    assert (
        context.validation_minio_locked_bucket
        == "phase10-m7b-abcdef12-validation-locked"
    )
    assert (
        context.validation_minio_missing_bucket
        == "phase10-m7b-abcdef12-validation-missing"
    )
    assert context.opensearch_index == "phase10-m7b-abcdef12-v1"
    assert (
        context.validation_opensearch_rollover_index
        == "phase10-m7b-abcdef12-validation-v2"
    )
    assert context.opensearch_alias == "phase10-m7b-abcdef12-current"
    assert context.document_prefix == "phase10-hard-delete-abcdef12-"


def test_run_context_derives_validation_and_rollout_domains_from_one_token() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")

    assert (
        context.postgres_database_for(Phase10ResourceDomain.VALIDATION)
        == "phase10_m7b_validation_abcdef12"
    )
    assert (
        context.postgres_database_for(Phase10ResourceDomain.ROLLOUT)
        == "phase10_m7b_abcdef12"
    )
    assert (
        context.minio_bucket_for(Phase10ResourceDomain.VALIDATION)
        == "phase10-m7b-abcdef12-validation"
    )
    assert (
        context.minio_bucket_for(Phase10ResourceDomain.ROLLOUT)
        == "phase10-m7b-abcdef12"
    )
    assert (
        context.opensearch_index_for(Phase10ResourceDomain.VALIDATION)
        == "phase10-m7b-abcdef12-validation-v1"
    )
    assert (
        context.opensearch_index_for(Phase10ResourceDomain.ROLLOUT)
        == "phase10-m7b-abcdef12-v1"
    )
    assert (
        context.opensearch_alias_for(Phase10ResourceDomain.VALIDATION)
        == "phase10-m7b-abcdef12-validation-current"
    )
    assert (
        context.opensearch_alias_for(Phase10ResourceDomain.ROLLOUT)
        == "phase10-m7b-abcdef12-current"
    )
    assert (
        context.document_prefix_for(Phase10ResourceDomain.VALIDATION)
        == "phase10-hard-delete-abcdef12-validation-"
    )
    assert (
        context.document_prefix_for(Phase10ResourceDomain.ROLLOUT)
        == "phase10-hard-delete-abcdef12-"
    )


def test_run_context_all_postgresql_databases_are_pairwise_distinct() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")

    assert len(
        {
            context.postgres_database_for(Phase10ResourceDomain.VALIDATION),
            context.postgres_database_for(Phase10ResourceDomain.ROLLOUT),
            context.migration_database,
            context.restore_database,
        }
    ) == 4


def test_run_context_rejects_unknown_resource_domain_without_fallback() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")

    with pytest.raises(Phase10IntegrationRunContextError, match="resource domain"):
        context.minio_bucket_for("validation")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "configured"),
    [
        (
            "validation_postgres_database",
            "phase10_m7b_abcdef12",
        ),
        (
            "validation_minio_bucket",
            "phase10-m7b-abcdef12",
        ),
        (
            "validation_opensearch_index",
            "phase10-m7b-abcdef12-v1",
        ),
        (
            "validation_opensearch_alias",
            "phase10-m7b-abcdef12-current",
        ),
    ],
)
def test_run_context_rejects_cross_domain_resource_swap(
    field: str,
    configured: str,
) -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")
    resources = {
        "validation_postgres_database": context.postgres_database_for(
            Phase10ResourceDomain.VALIDATION
        ),
        "rollout_postgres_database": context.postgres_database_for(
            Phase10ResourceDomain.ROLLOUT
        ),
        "migration_database": context.migration_database,
        "restore_database": context.restore_database,
        "validation_minio_bucket": context.minio_bucket_for(
            Phase10ResourceDomain.VALIDATION
        ),
        "rollout_minio_bucket": context.minio_bucket_for(
            Phase10ResourceDomain.ROLLOUT
        ),
        "validation_opensearch_index": context.opensearch_index_for(
            Phase10ResourceDomain.VALIDATION
        ),
        "rollout_opensearch_index": context.opensearch_index_for(
            Phase10ResourceDomain.ROLLOUT
        ),
        "validation_opensearch_alias": context.opensearch_alias_for(
            Phase10ResourceDomain.VALIDATION
        ),
        "rollout_opensearch_alias": context.opensearch_alias_for(
            Phase10ResourceDomain.ROLLOUT
        ),
    }
    resources[field] = configured

    with pytest.raises(Phase10IntegrationRunContextError, match="mismatch"):
        context.validate_configured_resources(**resources)


@pytest.mark.parametrize(
    "run_token",
    [
        "abcdef12",
        "0123456789abcdef",
        "a1b2c3d4e5f6",
        "a" * 32,
    ],
)
def test_run_context_accepts_only_valid_lowercase_hex_tokens(run_token: str) -> None:
    assert Phase10IntegrationRunContext(run_token=run_token).run_token == run_token


@pytest.mark.parametrize(
    "run_token",
    [
        "",
        "BADTOKEN",
        "abc-def1",
        "abc_def1",
        "../abc123",
        "abc 1234",
        "abc/1234",
        "abc1234",
        "a" * 33,
    ],
)
def test_run_context_rejects_invalid_tokens_without_normalizing(run_token: str) -> None:
    with pytest.raises(Phase10IntegrationRunContextError, match="run token"):
        Phase10IntegrationRunContext(run_token=run_token)


def test_run_context_database_names_are_pairwise_distinct() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")

    assert len(
        {
            context.postgres_database,
            context.migration_database,
            context.restore_database,
        }
    ) == 3


def test_run_context_safe_summary_contains_identities_and_no_connections() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")

    assert context.safe_summary() == {
        "run_token": "abcdef12",
        "validation_postgres_database": "phase10_m7b_validation_abcdef12",
        "rollout_postgres_database": "phase10_m7b_abcdef12",
        "migration_database": "phase10_m7b_migration_abcdef12",
        "restore_database": "phase10_m7b_restore_abcdef12",
        "validation_minio_bucket": "phase10-m7b-abcdef12-validation",
        "rollout_minio_bucket": "phase10-m7b-abcdef12",
        "validation_minio_locked_bucket": "phase10-m7b-abcdef12-validation-locked",
        "validation_minio_missing_bucket": "phase10-m7b-abcdef12-validation-missing",
        "validation_opensearch_index": "phase10-m7b-abcdef12-validation-v1",
        "validation_opensearch_rollover_index": "phase10-m7b-abcdef12-validation-v2",
        "rollout_opensearch_index": "phase10-m7b-abcdef12-v1",
        "validation_opensearch_alias": "phase10-m7b-abcdef12-validation-current",
        "rollout_opensearch_alias": "phase10-m7b-abcdef12-current",
        "validation_document_prefix": "phase10-hard-delete-abcdef12-validation-",
        "rollout_document_prefix": "phase10-hard-delete-abcdef12-",
    }


@pytest.mark.parametrize(
    ("field", "overrides", "message"),
    (
        (
            "validation_minio_locked_bucket",
            {
                "validation_minio_locked_bucket": (
                    "phase10-m7b-deadbeef-validation-locked"
                )
            },
            "locked bucket",
        ),
        (
            "validation_minio_missing_bucket",
            {
                "validation_minio_missing_bucket": (
                    "phase10-m7b-deadbeef-validation-missing"
                )
            },
            "missing bucket",
        ),
        (
            "validation_opensearch_rollover_index",
            {
                "validation_opensearch_rollover_index": (
                    "phase10-m7b-deadbeef-validation-v2"
                )
            },
            "rollover index",
        ),
    ),
)
def test_run_context_rejects_storage_extension_cross_run_mismatch(
    field: str,
    overrides: dict[str, str],
    message: str,
) -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")
    configured = {
        "validation_minio_locked_bucket": context.validation_minio_locked_bucket,
        "validation_minio_missing_bucket": context.validation_minio_missing_bucket,
        "validation_opensearch_rollover_index": (
            context.validation_opensearch_rollover_index
        ),
    }
    configured.update(overrides)

    assert configured[field] != getattr(context, field)
    with pytest.raises(Phase10IntegrationRunContextError, match=message):
        context.validate_storage_extension_resources(**configured)
