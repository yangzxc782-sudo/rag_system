from __future__ import annotations

import pytest

from integration.phase10_run_context import (
    Phase10IntegrationRunContext,
    Phase10IntegrationRunContextError,
)


def test_run_context_derives_every_resource_name_from_one_token() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")

    assert context.postgres_database == "phase10_m7b_abcdef12"
    assert context.migration_database == "phase10_m7b_migration_abcdef12"
    assert context.restore_database == "phase10_m7b_restore_abcdef12"
    assert context.minio_bucket == "phase10-m7b-abcdef12"
    assert context.minio_locked_bucket == "phase10-m7b-abcdef12-locked"
    assert context.minio_missing_bucket == "phase10-m7b-abcdef12-missing"
    assert context.opensearch_index == "phase10-m7b-abcdef12-v1"
    assert context.opensearch_rollover_index == "phase10-m7b-abcdef12-v2"
    assert context.opensearch_alias == "phase10-m7b-abcdef12-current"
    assert context.document_prefix == "phase10-hard-delete-abcdef12-"


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
        "R5TOKEN12",
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
        "postgres_database": "phase10_m7b_abcdef12",
        "migration_database": "phase10_m7b_migration_abcdef12",
        "restore_database": "phase10_m7b_restore_abcdef12",
        "minio_bucket": "phase10-m7b-abcdef12",
        "minio_locked_bucket": "phase10-m7b-abcdef12-locked",
        "minio_missing_bucket": "phase10-m7b-abcdef12-missing",
        "opensearch_index": "phase10-m7b-abcdef12-v1",
        "opensearch_rollover_index": "phase10-m7b-abcdef12-v2",
        "opensearch_alias": "phase10-m7b-abcdef12-current",
        "document_prefix": "phase10-hard-delete-abcdef12-",
    }


@pytest.mark.parametrize(
    ("field", "overrides", "message"),
    (
        (
            "minio_locked_bucket",
            {"minio_locked_bucket": "phase10-m7b-deadbeef-locked"},
            "locked bucket",
        ),
        (
            "minio_missing_bucket",
            {"minio_missing_bucket": "phase10-m7b-deadbeef-missing"},
            "missing bucket",
        ),
        (
            "opensearch_rollover_index",
            {"opensearch_rollover_index": "phase10-m7b-deadbeef-v2"},
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
        "minio_locked_bucket": context.minio_locked_bucket,
        "minio_missing_bucket": context.minio_missing_bucket,
        "opensearch_rollover_index": context.opensearch_rollover_index,
    }
    configured.update(overrides)

    assert configured[field] != getattr(context, field)
    with pytest.raises(Phase10IntegrationRunContextError, match=message):
        context.validate_storage_extension_resources(**configured)
