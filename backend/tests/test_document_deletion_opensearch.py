from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.search_engine import client as search_client
from app.services.document_deletion_manifest import DocumentDeletionManifest
from app.services.document_deletion_opensearch import (
    delete_opensearch_targets as delete_opensearch_targets_impl,
)
from app.services.document_deletion_storage import (
    DOCUMENT_DELETION_LEASE_LOST,
    DOCUMENT_DELETION_OPENSEARCH_DELETE_FAILED,
    DOCUMENT_DELETION_OPENSEARCH_INCOMPLETE,
    DOCUMENT_DELETION_OPENSEARCH_UNAVAILABLE,
    DocumentDeletionLeaseLost,
    DocumentDeletionStorageError,
)


DOCUMENT_ID = UUID("11111111-1111-1111-1111-111111111111")
CHUNK_ID = UUID("33333333-3333-3333-3333-333333333333")
CURRENT_INDEX_NAME = "casting_chunks_v1"
CURRENT_INDEX_ALIAS = "casting_chunks_current"


def manifest(
    *,
    chunk_ids: list[UUID] | None = None,
    index_name: str = "casting_chunks_v1",
    index_alias: str = "casting_chunks_current",
) -> DocumentDeletionManifest:
    return DocumentDeletionManifest.from_payload(
        {
            "schema_version": 1,
            "document_id": str(DOCUMENT_ID),
            "bucket_name": "rag-documents",
            "raw_object_key": f"raw/2026/08/{DOCUMENT_ID}.pdf",
            "derived_object_keys": [],
            "derived_prefixes": [],
            "parse_run_ids": [],
            "block_ids": [],
            "asset_ids": [],
            "chunk_ids": [str(value) for value in (chunk_ids or [CHUNK_ID])],
            "knowledge_source_relation_ids": [],
            "knowledge_item_ids": [],
            "search_index_name": index_name,
            "search_index_alias": index_alias,
        }
    )


class StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__("external error body must not escape")
        self.status_code = status_code


class FakeIndices:
    def __init__(
        self,
        events: list[str],
        *,
        physical_exists: bool = True,
        existing_indices: set[str] | None = None,
        aliases: dict[str, list[str]] | None = None,
    ) -> None:
        self.events = events
        self.physical_exists = physical_exists
        self.existing_indices = existing_indices
        self.aliases = aliases if aliases is not None else {
            "casting_chunks_current": ["casting_chunks_v1"]
        }
        self.error: Exception | None = None

    def exists(
        self,
        *,
        index: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ) -> bool:
        del params, headers
        self.events.append(f"exists:{index}")
        if self.error is not None:
            raise self.error
        if self.existing_indices is not None:
            return index in self.existing_indices
        return self.physical_exists if index == "casting_chunks_v1" else True

    def exists_alias(
        self,
        *,
        name: str,
        index: str | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ) -> bool:
        del index, params, headers
        self.events.append(f"exists_alias:{name}")
        if self.error is not None:
            raise self.error
        return name in self.aliases

    def get_alias(
        self,
        *,
        index: str | None = None,
        name: str | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        del index, params, headers
        assert name is not None
        self.events.append(f"get_alias:{name}")
        if self.error is not None:
            raise self.error
        return {
            concrete: {"aliases": {name: {}}}
            for concrete in self.aliases.get(name, [])
        }


class FakeOpenSearch:
    def __init__(
        self,
        *,
        physical_exists: bool = True,
        existing_indices: set[str] | None = None,
        aliases: dict[str, list[str]] | None = None,
        rollover_on_delete: tuple[str, list[str]] | None = None,
    ) -> None:
        self.events: list[str] = []
        self.indices = FakeIndices(
            self.events,
            physical_exists=physical_exists,
            existing_indices=existing_indices,
            aliases=aliases,
        )
        self.delete_calls: list[dict[str, object]] = []
        self.count_calls: list[dict[str, object]] = []
        self.delete_response: dict[str, object] = {
            "deleted": 0,
            "timed_out": False,
            "failures": [],
        }
        self.delete_error: Exception | None = None
        self.count_results: list[int] = []
        self.count_error: Exception | None = None
        self.rollover_on_delete = rollover_on_delete

    def delete_by_query(
        self,
        *,
        index: str,
        body: dict[str, Any],
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        del headers
        self.events.append(f"dbq:{index}")
        self.delete_calls.append({"index": index, "body": body, "params": params or {}})
        if self.delete_error is not None:
            raise self.delete_error
        if self.rollover_on_delete is not None:
            alias, targets = self.rollover_on_delete
            self.indices.aliases[alias] = targets
            self.rollover_on_delete = None
        return self.delete_response

    def count(
        self,
        *,
        body: dict[str, Any] | None = None,
        index: str | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        del params, headers
        assert body is not None and index is not None
        self.events.append(f"count:{index}")
        self.count_calls.append({"index": index, "body": body})
        if self.count_error is not None:
            raise self.count_error
        return {"count": self.count_results.pop(0) if self.count_results else 0}


def always_owned() -> bool:
    return True


def delete_opensearch_targets(
    deletion_manifest: DocumentDeletionManifest,
    *,
    client: FakeOpenSearch,
    checkpoint: Any,
    current_index_name: str = CURRENT_INDEX_NAME,
    current_index_alias: str = CURRENT_INDEX_ALIAS,
    **kwargs: Any,
) -> None:
    delete_opensearch_targets_impl(
        deletion_manifest,
        client=client,
        checkpoint=checkpoint,
        current_index_name=current_index_name,
        current_index_alias=current_index_alias,
        **kwargs,
    )


def test_document_delete_uses_exact_term_waits_refreshes_and_verifies_zero_hits() -> None:
    client = FakeOpenSearch()

    delete_opensearch_targets(
        manifest(),
        client=client,
        checkpoint=always_owned,
        timeout_seconds=30,
    )

    assert len(client.delete_calls) == 1
    call = client.delete_calls[0]
    assert call["index"] == "casting_chunks_v1"
    assert call["body"] == {
        "query": {"term": {"document_id": str(DOCUMENT_ID)}}
    }
    assert call["params"] == {
        "conflicts": "proceed",
        "refresh": True,
        "request_timeout": 30,
        "wait_for_completion": True,
    }
    assert client.count_calls[0]["body"] == {
        "query": {"term": {"document_id": str(DOCUMENT_ID)}}
    }
    chunk_query = client.count_calls[1]["body"]["query"]
    assert chunk_query["bool"]["minimum_should_match"] == 1
    assert {str(CHUNK_ID)} == set(chunk_query["bool"]["should"][0]["ids"]["values"])
    assert {str(CHUNK_ID)} == set(chunk_query["bool"]["should"][1]["terms"]["chunk_id"])


def test_alias_and_physical_target_resolving_to_same_index_are_deduplicated() -> None:
    client = FakeOpenSearch()

    delete_opensearch_targets(manifest(), client=client, checkpoint=always_owned)

    assert [call["index"] for call in client.delete_calls] == ["casting_chunks_v1"]
    assert {call["index"] for call in client.count_calls} == {"casting_chunks_v1"}


def test_alias_pointing_to_another_concrete_index_is_also_cleaned() -> None:
    client = FakeOpenSearch(
        aliases={
            "casting_chunks_current": ["casting_chunks_v1", "casting_chunks_v2"]
        }
    )

    delete_opensearch_targets(manifest(), client=client, checkpoint=always_owned)

    assert [call["index"] for call in client.delete_calls] == [
        "casting_chunks_v1",
        "casting_chunks_v2",
    ]


def test_manifest_and_renamed_current_targets_are_deleted_as_a_union() -> None:
    client = FakeOpenSearch(
        existing_indices={"casting_chunks_v1", "casting_chunks_v2"},
        aliases={
            "casting_chunks_current": ["casting_chunks_v1"],
            "casting_chunks_current_v2": ["casting_chunks_v2"],
        },
    )

    delete_opensearch_targets(
        manifest(),
        client=client,
        checkpoint=always_owned,
        current_index_name="casting_chunks_v2",
        current_index_alias="casting_chunks_current_v2",
    )

    assert [call["index"] for call in client.delete_calls] == [
        "casting_chunks_v1",
        "casting_chunks_v2",
    ]


def test_manifest_and_current_aliases_resolving_same_concrete_delete_once() -> None:
    client = FakeOpenSearch(
        existing_indices={"casting_chunks_v1"},
        aliases={
            "casting_chunks_current": ["casting_chunks_v1"],
            "casting_chunks_current_v2": ["casting_chunks_v1"],
        },
    )

    delete_opensearch_targets(
        manifest(),
        client=client,
        checkpoint=always_owned,
        current_index_name="casting_chunks_v1",
        current_index_alias="casting_chunks_current_v2",
    )

    assert [call["index"] for call in client.delete_calls] == ["casting_chunks_v1"]


def test_missing_manifest_alias_does_not_hide_valid_current_alias_target() -> None:
    client = FakeOpenSearch(
        existing_indices={"casting_chunks_v1", "casting_chunks_v2"},
        aliases={"casting_chunks_current_v2": ["casting_chunks_v2"]},
    )

    delete_opensearch_targets(
        manifest(),
        client=client,
        checkpoint=always_owned,
        current_index_name="missing_current_physical",
        current_index_alias="casting_chunks_current_v2",
    )

    assert [call["index"] for call in client.delete_calls] == [
        "casting_chunks_v1",
        "casting_chunks_v2",
    ]


def test_missing_manifest_physical_does_not_hide_valid_current_physical() -> None:
    client = FakeOpenSearch(
        existing_indices={"casting_chunks_v2"},
        aliases={},
    )

    delete_opensearch_targets(
        manifest(),
        client=client,
        checkpoint=always_owned,
        current_index_name="casting_chunks_v2",
        current_index_alias="casting_chunks_current_v2",
    )

    assert [call["index"] for call in client.delete_calls] == ["casting_chunks_v2"]


def test_alias_rollover_between_delete_and_verification_is_not_silent_success() -> None:
    client = FakeOpenSearch(
        existing_indices={"casting_chunks_v1", "casting_chunks_v2"},
        aliases={"casting_chunks_current": ["casting_chunks_v1"]},
        rollover_on_delete=("casting_chunks_current", ["casting_chunks_v2"]),
    )

    with pytest.raises(DocumentDeletionStorageError) as exc_info:
        delete_opensearch_targets(
            manifest(),
            client=client,
            checkpoint=always_owned,
        )

    assert exc_info.value.code == DOCUMENT_DELETION_OPENSEARCH_INCOMPLETE
    assert [call["index"] for call in client.delete_calls] == ["casting_chunks_v1"]


def test_already_zero_hits_is_idempotent_success() -> None:
    client = FakeOpenSearch()
    client.delete_response = {"deleted": 0, "timed_out": False, "failures": []}

    delete_opensearch_targets(manifest(), client=client, checkpoint=always_owned)

    assert len(client.delete_calls) == 1
    assert all(call for call in client.count_calls)


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        ({"deleted": 1, "timed_out": True, "failures": []}, DOCUMENT_DELETION_OPENSEARCH_DELETE_FAILED),
        (
            {"deleted": 1, "timed_out": False, "failures": [{"reason": "secret"}]},
            DOCUMENT_DELETION_OPENSEARCH_DELETE_FAILED,
        ),
    ],
)
def test_dbq_timeout_or_failures_are_not_completion(
    response: dict[str, object],
    error_code: str,
) -> None:
    client = FakeOpenSearch()
    client.delete_response = response

    with pytest.raises(DocumentDeletionStorageError) as exc_info:
        delete_opensearch_targets(manifest(), client=client, checkpoint=always_owned)

    assert exc_info.value.code == error_code
    assert "secret" not in str(exc_info.value)
    assert client.count_calls == []


def test_connection_or_auth_error_is_not_treated_as_absence() -> None:
    client = FakeOpenSearch()
    client.delete_error = ConnectionError("credentials and endpoint must stay private")

    with pytest.raises(DocumentDeletionStorageError) as exc_info:
        delete_opensearch_targets(manifest(), client=client, checkpoint=always_owned)

    assert exc_info.value.code == DOCUMENT_DELETION_OPENSEARCH_UNAVAILABLE
    assert "credentials" not in str(exc_info.value)


def test_missing_physical_index_and_alias_is_absence_success() -> None:
    client = FakeOpenSearch(physical_exists=False, aliases={})

    delete_opensearch_targets(manifest(), client=client, checkpoint=always_owned)

    assert client.delete_calls == []
    assert client.count_calls == []


def test_missing_alias_after_physical_verification_is_success() -> None:
    client = FakeOpenSearch(physical_exists=True, aliases={})

    delete_opensearch_targets(manifest(), client=client, checkpoint=always_owned)

    assert [call["index"] for call in client.delete_calls] == ["casting_chunks_v1"]


def test_index_disappearing_during_dbq_is_idempotent_success() -> None:
    client = FakeOpenSearch()
    client.delete_error = StatusError(404)

    delete_opensearch_targets(manifest(), client=client, checkpoint=always_owned)

    assert len(client.delete_calls) == 1


def test_nonzero_document_verification_is_retryable_incomplete() -> None:
    client = FakeOpenSearch()
    client.count_results = [1]

    with pytest.raises(DocumentDeletionStorageError) as exc_info:
        delete_opensearch_targets(manifest(), client=client, checkpoint=always_owned)

    assert exc_info.value.code == DOCUMENT_DELETION_OPENSEARCH_INCOMPLETE


def test_nonzero_chunk_verification_is_retryable_incomplete() -> None:
    client = FakeOpenSearch()
    client.count_results = [0, 1]

    with pytest.raises(DocumentDeletionStorageError) as exc_info:
        delete_opensearch_targets(manifest(), client=client, checkpoint=always_owned)

    assert exc_info.value.code == DOCUMENT_DELETION_OPENSEARCH_INCOMPLETE


def test_heartbeat_runs_before_and_after_dbq_and_before_verification() -> None:
    client = FakeOpenSearch()

    def checkpoint() -> bool:
        client.events.append("heartbeat")
        return True

    delete_opensearch_targets(manifest(), client=client, checkpoint=checkpoint)

    dbq_index = client.events.index("dbq:casting_chunks_v1")
    first_count_index = client.events.index("count:casting_chunks_v1")
    assert client.events[dbq_index - 1] == "heartbeat"
    assert client.events[dbq_index + 1] == "heartbeat"
    assert client.events[first_count_index - 1] == "heartbeat"


def test_lease_lost_before_resolution_starts_no_external_call() -> None:
    client = FakeOpenSearch()

    with pytest.raises(DocumentDeletionLeaseLost) as exc_info:
        delete_opensearch_targets(
            manifest(),
            client=client,
            checkpoint=lambda: False,
        )

    assert exc_info.value.code == DOCUMENT_DELETION_LEASE_LOST
    assert client.events == []


def test_lease_lost_after_dbq_stops_completion_verification() -> None:
    client = FakeOpenSearch()

    def checkpoint() -> bool:
        return not any(event.startswith("dbq:") for event in client.events)

    with pytest.raises(DocumentDeletionLeaseLost):
        delete_opensearch_targets(manifest(), client=client, checkpoint=checkpoint)

    assert len(client.delete_calls) == 1
    assert client.count_calls == []


def test_deletion_opensearch_client_is_bounded_and_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeSdkClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    fake_module = types.ModuleType("opensearchpy")
    fake_module.OpenSearch = FakeSdkClient
    monkeypatch.setitem(sys.modules, "opensearchpy", fake_module)
    settings = SimpleNamespace(
        search_engine_provider="opensearch",
        search_engine_url="http://localhost:9200",
        search_engine_username="",
        search_engine_password="",
        search_engine_verify_ssl=False,
        search_engine_timeout_seconds=90,
        search_engine_max_retries=3,
        document_deletion_storage_timeout_seconds=30,
    )

    search_client.create_document_deletion_search_engine_client(settings)

    assert captured["timeout"] == 30
    assert captured["max_retries"] == 0
    assert captured["retry_on_timeout"] is False


def test_deletion_storage_timeout_defaults_to_30_and_must_be_positive() -> None:
    assert Settings(_env_file=None).document_deletion_storage_timeout_seconds == 30

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            document_deletion_storage_timeout_seconds=0,
        )
