from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from types import SimpleNamespace
from uuid import UUID

import pytest
from minio.datatypes import Object
from minio.deleteobjects import DeleteError, DeleteObject
from minio.error import S3Error

from app.services import object_storage
from app.services.document_deletion_manifest import DocumentDeletionManifest
from app.services.document_deletion_minio import (
    delete_minio_derived_targets,
    delete_minio_raw_target,
    delete_minio_targets,
)
from app.services.document_deletion_storage import (
    DOCUMENT_DELETION_LEASE_LOST,
    DOCUMENT_DELETION_MINIO_BUCKET_NOT_FOUND,
    DOCUMENT_DELETION_MINIO_DELETE_FAILED,
    DOCUMENT_DELETION_MINIO_UNAVAILABLE,
    DocumentDeletionLeaseLost,
    DocumentDeletionStorageError,
)


DOCUMENT_ID = UUID("11111111-1111-1111-1111-111111111111")
PARSE_RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
PREFIX = f"parsed-assets/{DOCUMENT_ID}/{PARSE_RUN_ID}/"
RAW_KEY = f"raw/2026/08/{DOCUMENT_ID}.pdf"
HEARTBEAT_INTERVAL_SECONDS = 40.0


def manifest() -> DocumentDeletionManifest:
    return DocumentDeletionManifest.from_payload(
        {
            "schema_version": 1,
            "document_id": str(DOCUMENT_ID),
            "bucket_name": "rag-documents",
            "raw_object_key": RAW_KEY,
            "derived_object_keys": [f"{PREFIX}output.md", f"{PREFIX}images/mould.png"],
            "derived_prefixes": [PREFIX],
            "parse_run_ids": [str(PARSE_RUN_ID)],
            "block_ids": [],
            "asset_ids": [],
            "chunk_ids": [],
            "knowledge_source_relation_ids": [],
            "knowledge_item_ids": [],
            "search_index_name": "casting_chunks_v1",
            "search_index_alias": "casting_chunks_current",
        }
    )


def object_version(
    key: str,
    version_id: str | None,
    *,
    delete_marker: bool = False,
) -> Object:
    return Object(
        bucket_name="rag-documents",
        object_name=key,
        version_id=version_id,
        is_delete_marker=delete_marker,
    )


def s3_error(code: str) -> S3Error:
    return S3Error(
        response=SimpleNamespace(),
        code=code,
        message="external detail must not escape",
        resource=None,
        request_id=None,
        host_id=None,
        bucket_name="rag-documents",
    )


class FakeMinio:
    def __init__(self, objects: Iterable[Object] = ()) -> None:
        self.objects = list(objects)
        self.list_calls: list[dict[str, object]] = []
        self.remove_calls: list[list[DeleteObject]] = []
        self.list_error: Exception | None = None
        self.remove_error_batches: list[list[DeleteError]] = []
        self.remove_exception: Exception | None = None
        self.error_iterator_consumed = False
        self.yielded_objects = 0
        self.on_list_yield: Callable[[int], None] | None = None

    def list_objects(
        self,
        bucket_name: str,
        prefix: str | None = None,
        recursive: bool = False,
        start_after: str | None = None,
        include_user_meta: bool = False,
        include_version: bool = False,
        use_api_v1: bool = False,
        use_url_encoding_type: bool = True,
        fetch_owner: bool = False,
        extra_headers: dict[str, str] | None = None,
        extra_query_params: dict[str, str] | None = None,
    ) -> Iterator[Object]:
        del start_after, include_user_meta, use_api_v1, use_url_encoding_type
        del fetch_owner, extra_headers, extra_query_params
        self.list_calls.append(
            {
                "bucket_name": bucket_name,
                "prefix": prefix,
                "recursive": recursive,
                "include_version": include_version,
            }
        )

        def iterator() -> Iterator[Object]:
            if self.list_error is not None:
                raise self.list_error
            for item in self.objects:
                if item.object_name is None or (
                    prefix is not None and not item.object_name.startswith(prefix)
                ):
                    continue
                self.yielded_objects += 1
                if self.on_list_yield is not None:
                    self.on_list_yield(self.yielded_objects)
                yield item

        return iterator()

    def remove_objects(
        self,
        bucket_name: str,
        delete_object_list: Iterable[DeleteObject],
        bypass_governance_mode: bool = False,
    ) -> Iterator[DeleteError]:
        del bucket_name, bypass_governance_mode
        targets = list(delete_object_list)
        self.remove_calls.append(targets)
        errors = self.remove_error_batches.pop(0) if self.remove_error_batches else []
        failed = {(error.name, error.version_id) for error in errors}
        self.objects = [
            item
            for item in self.objects
            if (item.object_name, item.version_id)
            not in {(target.name, target.version_id) for target in targets}
            or (item.object_name, item.version_id) in failed
        ]

        def iterator() -> Iterator[DeleteError]:
            if self.remove_exception is not None:
                raise self.remove_exception
            try:
                yield from errors
            finally:
                self.error_iterator_consumed = True

        return iterator()


def always_owned() -> bool:
    return True


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_exact_raw_delete_removes_only_the_exact_object() -> None:
    client = FakeMinio(
        [
            object_version(RAW_KEY, None),
            object_version(f"{RAW_KEY}.neighbor", None),
        ]
    )

    delete_minio_raw_target(
        manifest(),
        client=client,
        checkpoint=always_owned,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )

    assert [(item.object_name, item.version_id) for item in client.objects] == [
        (f"{RAW_KEY}.neighbor", None)
    ]
    assert all(call["prefix"] == RAW_KEY for call in client.list_calls)
    assert all(call["include_version"] is True for call in client.list_calls)


def test_derived_delete_removes_normal_versions_and_delete_markers() -> None:
    client = FakeMinio(
        [
            object_version(f"{PREFIX}output.md", "v2"),
            object_version(f"{PREFIX}output.md", "v1"),
            object_version(f"{PREFIX}output.md", "marker", delete_marker=True),
            object_version(f"{PREFIX}images/mould.png", None),
            object_version(f"parsed-assets/{DOCUMENT_ID}/other-run/keep.png", "keep"),
        ]
    )

    delete_minio_derived_targets(
        manifest(),
        client=client,
        checkpoint=always_owned,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )

    assert [(item.object_name, item.version_id) for item in client.objects] == [
        (f"parsed-assets/{DOCUMENT_ID}/other-run/keep.png", "keep")
    ]
    removed = {
        (target.name, target.version_id)
        for batch in client.remove_calls
        for target in batch
    }
    assert removed >= {
        (f"{PREFIX}output.md", "v2"),
        (f"{PREFIX}output.md", "v1"),
        (f"{PREFIX}output.md", "marker"),
        (f"{PREFIX}images/mould.png", None),
    }


def test_delete_all_is_idempotent_when_every_target_is_already_absent() -> None:
    client = FakeMinio()

    delete_minio_targets(
        manifest(),
        client=client,
        checkpoint=always_owned,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )
    delete_minio_targets(
        manifest(),
        client=client,
        checkpoint=always_owned,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )

    assert client.remove_calls == []
    assert client.list_calls


def test_partial_delete_is_retryable_and_second_run_converges() -> None:
    blocked = DeleteError(
        "AccessDenied",
        "retention active",
        f"{PREFIX}images/mould.png",
        "v1",
    )
    client = FakeMinio(
        [
            object_version(f"{PREFIX}output.md", "v1"),
            object_version(f"{PREFIX}images/mould.png", "v1"),
        ]
    )
    client.remove_error_batches = [[blocked]]

    with pytest.raises(DocumentDeletionStorageError) as exc_info:
        delete_minio_derived_targets(
            manifest(),
            client=client,
            checkpoint=always_owned,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        )

    assert exc_info.value.code == DOCUMENT_DELETION_MINIO_DELETE_FAILED
    assert [(item.object_name, item.version_id) for item in client.objects] == [
        (f"{PREFIX}images/mould.png", "v1")
    ]

    delete_minio_derived_targets(
        manifest(),
        client=client,
        checkpoint=always_owned,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )

    assert client.objects == []


@pytest.mark.parametrize("missing_code", ["NoSuchKey", "NoSuchObject"])
def test_missing_object_is_idempotent_success(missing_code: str) -> None:
    client = FakeMinio()
    client.list_error = s3_error(missing_code)

    delete_minio_raw_target(
        manifest(),
        client=client,
        checkpoint=always_owned,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )

    assert client.objects == []


def test_no_such_bucket_is_not_treated_as_deleted() -> None:
    client = FakeMinio()
    client.list_error = s3_error("NoSuchBucket")

    with pytest.raises(DocumentDeletionStorageError) as exc_info:
        delete_minio_raw_target(
            manifest(),
            client=client,
            checkpoint=always_owned,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        )

    assert exc_info.value.code == DOCUMENT_DELETION_MINIO_BUCKET_NOT_FOUND
    assert "external detail" not in str(exc_info.value)


@pytest.mark.parametrize("error", [ConnectionError("offline"), s3_error("InvalidAccessKeyId")])
def test_network_or_auth_failure_is_retryable_storage_failure(error: Exception) -> None:
    client = FakeMinio()
    client.list_error = error

    with pytest.raises(DocumentDeletionStorageError) as exc_info:
        delete_minio_raw_target(
            manifest(),
            client=client,
            checkpoint=always_owned,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        )

    assert exc_info.value.code == DOCUMENT_DELETION_MINIO_UNAVAILABLE
    assert "offline" not in str(exc_info.value)


def test_remove_objects_error_iterator_is_fully_consumed() -> None:
    client = FakeMinio(
        [
            object_version(f"{PREFIX}output.md", "v1"),
            object_version(f"{PREFIX}images/mould.png", "v1"),
        ]
    )
    client.remove_error_batches = [
        [
            DeleteError("AccessDenied", "first secret", f"{PREFIX}output.md", "v1"),
            DeleteError("AccessDenied", "second secret", f"{PREFIX}images/mould.png", "v1"),
        ]
    ]

    with pytest.raises(DocumentDeletionStorageError) as exc_info:
        delete_minio_derived_targets(
            manifest(),
            client=client,
            checkpoint=always_owned,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        )

    assert client.error_iterator_consumed is True
    assert exc_info.value.code == DOCUMENT_DELETION_MINIO_DELETE_FAILED
    assert "secret" not in str(exc_info.value)


def test_object_lock_or_retention_error_is_not_success() -> None:
    client = FakeMinio([object_version(RAW_KEY, "v1")])
    client.remove_error_batches = [
        [DeleteError("ObjectLocked", "retention", RAW_KEY, "v1")]
    ]

    with pytest.raises(DocumentDeletionStorageError) as exc_info:
        delete_minio_raw_target(
            manifest(),
            client=client,
            checkpoint=always_owned,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        )

    assert exc_info.value.code == DOCUMENT_DELETION_MINIO_DELETE_FAILED


def test_checkpoint_runs_during_long_version_listing_every_25_objects() -> None:
    client = FakeMinio(
        [object_version(f"{PREFIX}item-{index}.png", f"v-{index}") for index in range(51)]
    )
    checkpoints_before_first_delete: list[int] = []

    def checkpoint() -> bool:
        checkpoints_before_first_delete.append(len(client.remove_calls))
        return True

    delete_minio_derived_targets(
        manifest(),
        client=client,
        checkpoint=checkpoint,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )

    assert checkpoints_before_first_delete.count(0) >= 4
    assert client.objects == []


def test_elapsed_heartbeat_runs_before_25_objects_without_sleeping() -> None:
    clock = FakeMonotonic()
    client = FakeMinio(
        [object_version(f"{PREFIX}item-{index}.png", f"v-{index}") for index in range(20)]
    )
    client.on_list_yield = lambda count: (
        clock.advance(HEARTBEAT_INTERVAL_SECONDS) if count == 20 else None
    )
    checkpoint_events: list[tuple[int, int]] = []

    def checkpoint() -> bool:
        checkpoint_events.append((client.yielded_objects, len(client.remove_calls)))
        return True

    delete_minio_derived_targets(
        manifest(),
        client=client,
        checkpoint=checkpoint,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        monotonic=clock,
    )

    assert checkpoint_events.count((20, 0)) == 3


def test_count_heartbeat_runs_at_25_objects_before_elapsed_interval() -> None:
    clock = FakeMonotonic()
    client = FakeMinio(
        [object_version(f"{PREFIX}item-{index}.png", f"v-{index}") for index in range(25)]
    )
    checkpoint_events: list[tuple[int, int]] = []

    def checkpoint() -> bool:
        checkpoint_events.append((client.yielded_objects, len(client.remove_calls)))
        return True

    delete_minio_derived_targets(
        manifest(),
        client=client,
        checkpoint=checkpoint,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        monotonic=clock,
    )

    assert checkpoint_events.count((25, 0)) == 3


def test_no_extra_loop_heartbeat_before_count_or_elapsed_threshold() -> None:
    clock = FakeMonotonic()
    client = FakeMinio(
        [object_version(f"{PREFIX}item-{index}.png", f"v-{index}") for index in range(20)]
    )
    checkpoint_events: list[tuple[int, int]] = []

    def checkpoint() -> bool:
        checkpoint_events.append((client.yielded_objects, len(client.remove_calls)))
        return True

    delete_minio_derived_targets(
        manifest(),
        client=client,
        checkpoint=checkpoint,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        monotonic=clock,
    )

    assert checkpoint_events.count((20, 0)) == 2


def test_elapsed_heartbeat_lease_loss_stops_listing_before_next_object_or_delete() -> None:
    clock = FakeMonotonic()
    client = FakeMinio(
        [object_version(f"{PREFIX}item-{index}.png", f"v-{index}") for index in range(30)]
    )
    client.on_list_yield = lambda count: (
        clock.advance(HEARTBEAT_INTERVAL_SECONDS) if count == 20 else None
    )

    def checkpoint() -> bool:
        return client.yielded_objects < 20

    with pytest.raises(DocumentDeletionLeaseLost) as exc_info:
        delete_minio_derived_targets(
            manifest(),
            client=client,
            checkpoint=checkpoint,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            monotonic=clock,
        )

    assert exc_info.value.code == DOCUMENT_DELETION_LEASE_LOST
    assert client.yielded_objects == 20
    assert client.remove_calls == []
    assert len(client.objects) == 30


def test_lease_loss_after_first_delete_batch_stops_remaining_external_calls() -> None:
    client = FakeMinio(
        [object_version(f"{PREFIX}item-{index}.png", f"v-{index}") for index in range(3)]
    )

    def checkpoint() -> bool:
        return len(client.remove_calls) == 0

    with pytest.raises(DocumentDeletionLeaseLost) as exc_info:
        delete_minio_derived_targets(
            manifest(),
            client=client,
            checkpoint=checkpoint,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            delete_batch_size=1,
        )

    assert exc_info.value.code == DOCUMENT_DELETION_LEASE_LOST
    assert len(client.remove_calls) == 1
    assert len(client.objects) == 2


def test_deletion_minio_client_uses_bounded_timeout_and_no_sdk_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    settings = SimpleNamespace(
        minio_endpoint="localhost:9000",
        minio_root_user="user",
        minio_root_password="password",
        minio_secure=False,
        document_deletion_storage_timeout_seconds=30,
    )
    monkeypatch.setattr(object_storage, "Minio", FakeClient)

    object_storage.get_document_deletion_minio_client(settings=settings)

    http_client = captured["http_client"]
    timeout = http_client.connection_pool_kw["timeout"]
    retries = http_client.connection_pool_kw["retries"]
    assert timeout.connect_timeout == 30
    assert timeout.read_timeout == 30
    assert retries.total == 0
