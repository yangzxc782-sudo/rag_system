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
    assert context.opensearch_index == "phase10-m7b-abcdef12-v1"
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
        "opensearch_index": "phase10-m7b-abcdef12-v1",
        "opensearch_alias": "phase10-m7b-abcdef12-current",
        "document_prefix": "phase10-hard-delete-abcdef12-",
    }
