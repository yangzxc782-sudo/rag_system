from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import UUID, uuid4

from minio.commonconfig import COMPLIANCE
from minio.retention import Retention
from sqlalchemy import func, select, text, update

import pytest

from app.models.document import Document
from app.models.document_deletion_job import DocumentDeletionJob
from app.search_engine.index_schema import build_casting_chunks_index_body
from app.services.document_deletion import (
    ClaimedDocumentDeletion,
    DocumentDeletionSaga,
    advance_claimed_deletion,
    claim_document_deletion,
    renew_claimed_deletion,
)
from app.services.document_deletion_storage import (
    DOCUMENT_DELETION_MINIO_BUCKET_NOT_FOUND,
    DOCUMENT_DELETION_MINIO_DELETE_FAILED,
    DOCUMENT_DELETION_OPENSEARCH_UNAVAILABLE,
    DocumentDeletionLeaseLost,
    DocumentDeletionStorageError,
)
from app.tasks.document_deletion_executor import DocumentDeletionExecutor
from app.services.document_deletion_minio import (
    delete_minio_derived_targets,
    delete_minio_raw_target,
)
from app.services.document_deletion_opensearch import delete_opensearch_targets
from integration.phase10_fixtures import (
    build_test_embedding,
    build_storage_manifest,
    drain_claimable_jobs_excluding_target,
    index_opensearch_fixture,
    persist_document_fixture,
    schedule_pending_job,
    upload_minio_fixture_versions,
)
from integration.phase10_support import (
    Phase10ResourceSnapshot,
    assert_unrelated_resources_unchanged,
    capture_minio_snapshot,
    capture_opensearch_snapshot,
    stable_json_sha256,
)
from integration.phase10_storage_faults import OneShotPartialDeleteMinioClient


pytestmark = pytest.mark.integration


class SlowFakeMinio:
    """MinIO SDK-shaped fake; only PostgreSQL is real in heartbeat tests."""

    def __init__(self, object_keys: list[str], *, delay_seconds: float) -> None:
        self.objects = {
            (key, "v1"): SimpleNamespace(
                object_name=key,
                version_id="v1",
                is_delete_marker=False,
            )
            for key in object_keys
        }
        self.delay_seconds = delay_seconds
        self.list_calls = 0
        self.remove_calls = 0

    def list_objects(
        self,
        bucket_name: str,
        *,
        prefix: str,
        recursive: bool,
        include_version: bool,
    ):
        del bucket_name, recursive
        assert include_version is True
        self.list_calls += 1
        for (key, _version_id), entry in list(self.objects.items()):
            if key.startswith(prefix):
                time.sleep(self.delay_seconds)
                yield entry

    def remove_objects(self, bucket_name: str, delete_objects):
        del bucket_name
        self.remove_calls += 1
        for delete_object in delete_objects:
            self.objects.pop(
                (delete_object.name, delete_object.version_id),
                None,
            )
        return iter(())


class OneShotOpenSearchFault:
    def __init__(self, delegate, *, lose_response_after_delete: bool) -> None:
        self.delegate = delegate
        self.indices = delegate.indices
        self.lose_response_after_delete = lose_response_after_delete
        self.injected = False

    def delete_by_query(self, **kwargs):
        if self.injected:
            return self.delegate.delete_by_query(**kwargs)
        self.injected = True
        if self.lose_response_after_delete:
            self.delegate.delete_by_query(**kwargs)
        raise ConnectionError("controlled Phase 10 transport loss")

    def count(self, **kwargs):
        return self.delegate.count(**kwargs)

    def search(self, **kwargs):
        return self.delegate.search(**kwargs)

    def bulk(self, **kwargs):
        return self.delegate.bulk(**kwargs)


def _persist_processing_heartbeat_target(
    session,
    identity,
    settings,
    *,
    document_factory,
    locked_by: str,
    lease_seconds: int,
) -> ClaimedDocumentDeletion:
    job = schedule_pending_job(
        session,
        identity,
        settings,
        current_step="delete_minio_derived",
        document_factory=document_factory,
    )
    job.status = "processing"
    job.step_attempts = 1
    job.locked_by = locked_by
    job.lease_token = uuid4()
    job.locked_at = func.now()
    job.lease_expires_at = func.now() + text(
        f"INTERVAL '{int(lease_seconds)} seconds'"
    )
    session.flush()
    session.refresh(job)
    return ClaimedDocumentDeletion.from_job(job)


def test_fixture_setup_opensearch_accepts_non_zero_1024_dimension_vectors(
    phase10_settings,
    phase10_opensearch_client,
    phase10_document_factory,
) -> None:
    identity = phase10_document_factory.create()
    mapping = phase10_opensearch_client.indices.get_mapping(
        index=phase10_settings.opensearch_index
    )
    properties = mapping[phase10_settings.opensearch_index]["mappings"]["properties"]
    dimension = int(properties["embedding"]["dimension"])
    vector = build_test_embedding(dimension)
    assert dimension == 1024
    assert any(value != 0.0 for value in vector)

    index_opensearch_fixture(
        phase10_opensearch_client,
        identity,
        phase10_settings.opensearch_index,
        document_factory=phase10_document_factory,
    )

    document_hits = phase10_opensearch_client.count(
        index=phase10_settings.opensearch_index,
        body={"query": {"term": {"document_id": str(identity.document_id)}}},
    )["count"]
    assert document_hits == len(identity.chunk_ids)
    for chunk_id in identity.chunk_ids:
        assert phase10_opensearch_client.exists(
            index=phase10_settings.opensearch_index,
            id=str(chunk_id),
        )


def test_opensearch_36_accepts_production_document_deletion_query_parameters(
    phase10_settings,
    phase10_opensearch_client,
    phase10_document_factory,
) -> None:
    identity = phase10_document_factory.create()
    manifest = build_storage_manifest(
        identity,
        phase10_settings,
        document_factory=phase10_document_factory,
    )
    index_opensearch_fixture(
        phase10_opensearch_client,
        identity,
        phase10_settings.opensearch_index,
        document_factory=phase10_document_factory,
    )

    delete_opensearch_targets(
        manifest,
        client=phase10_opensearch_client,
        checkpoint=lambda: None,
        current_index_name=phase10_settings.opensearch_index,
        current_index_alias=phase10_settings.opensearch_alias,
        timeout_seconds=30,
    )

    assert phase10_opensearch_client.count(
        index=phase10_settings.opensearch_index,
        body={"query": {"term": {"document_id": str(identity.document_id)}}},
    )["count"] == 0


def test_fixture_setup_minio_lists_versions_and_delete_markers(
    phase10_settings,
    phase10_minio_client,
    phase10_document_factory,
) -> None:
    identity = phase10_document_factory.create()
    upload_minio_fixture_versions(
        phase10_minio_client,
        identity,
        phase10_settings.minio_bucket,
        document_factory=phase10_document_factory,
    )

    snapshot = capture_minio_snapshot(
        phase10_minio_client,
        phase10_settings.minio_bucket,
    )
    target_entries = tuple(
        entry for entry in snapshot.minio_versions if entry[0] in identity.all_minio_keys
    )
    assert {entry[0] for entry in target_entries} == set(identity.all_minio_keys)
    assert all(entry[1] for entry in target_entries)
    assert any(entry[2] for entry in target_entries)


def test_fixture_setup_heartbeat_contender_drain_rolls_back_unrelated_jobs(
    phase10_session_factory,
    phase10_settings,
    phase10_document_factory,
) -> None:
    target = phase10_document_factory.create()
    other_pending = phase10_document_factory.create()
    other_retry = phase10_document_factory.create()
    with phase10_session_factory() as session:
        for identity in (target, other_pending, other_retry):
            persist_document_fixture(
                session,
                identity,
                bucket_name=phase10_settings.minio_bucket,
                document_factory=phase10_document_factory,
            )
        claimed = _persist_processing_heartbeat_target(
            session,
            target,
            phase10_settings,
            document_factory=phase10_document_factory,
            locked_by="fixture-heartbeat-owner",
            lease_seconds=30,
        )
        pending_job = schedule_pending_job(
            session,
            other_pending,
            phase10_settings,
            current_step="delete_minio_derived",
            document_factory=phase10_document_factory,
        )
        retry_job = schedule_pending_job(
            session,
            other_retry,
            phase10_settings,
            current_step="delete_minio_derived",
            document_factory=phase10_document_factory,
        )
        retry_job.status = "retry_wait"
        retry_job.next_retry_at = func.now() - text("INTERVAL '1 second'")
        session.commit()
        session.refresh(pending_job)
        session.refresh(retry_job)
        existing_job_count = int(
            session.scalar(select(func.count()).select_from(DocumentDeletionJob)) or 0
        )
        target_token = claimed.lease_token

    with phase10_session_factory() as contender_session:
        temporarily_claimed = drain_claimable_jobs_excluding_target(
            contender_session,
            target_job_id=claimed.job_id,
            max_claims=existing_job_count + 2,
        )

    assert {pending_job.id, retry_job.id}.issubset(set(temporarily_claimed))
    with phase10_session_factory() as session:
        restored_pending = session.get(DocumentDeletionJob, pending_job.id)
        restored_retry = session.get(DocumentDeletionJob, retry_job.id)
        owned_target = session.get(DocumentDeletionJob, claimed.job_id)
        assert restored_pending is not None and restored_pending.status == "pending"
        assert restored_retry is not None and restored_retry.status == "retry_wait"
        assert owned_target is not None
        assert owned_target.status == "processing"
        assert owned_target.locked_by == "fixture-heartbeat-owner"
        assert owned_target.lease_token == target_token
        assert session.scalar(
            select(DocumentDeletionJob.lease_expires_at > func.now()).where(
                DocumentDeletionJob.id == claimed.job_id
            )
        ) is True


def test_real_storage_deleters_remove_target_versions_markers_and_hits_only(
    phase10_settings,
    phase10_minio_client,
    phase10_opensearch_client,
    phase10_document_factory,
) -> None:
    target = phase10_document_factory.create()
    control = phase10_document_factory.create()
    target_manifest = build_storage_manifest(
        target,
        phase10_settings,
        document_factory=phase10_document_factory,
    )
    control_manifest = build_storage_manifest(
        control,
        phase10_settings,
        document_factory=phase10_document_factory,
    )
    upload_minio_fixture_versions(
        phase10_minio_client,
        target,
        phase10_settings.minio_bucket,
        document_factory=phase10_document_factory,
    )
    upload_minio_fixture_versions(
        phase10_minio_client,
        control,
        phase10_settings.minio_bucket,
        document_factory=phase10_document_factory,
    )
    index_opensearch_fixture(
        phase10_opensearch_client,
        target,
        phase10_settings.opensearch_index,
        document_factory=phase10_document_factory,
    )
    index_opensearch_fixture(
        phase10_opensearch_client,
        control,
        phase10_settings.opensearch_index,
        document_factory=phase10_document_factory,
    )

    try:
        before_minio = capture_minio_snapshot(
            phase10_minio_client,
            phase10_settings.minio_bucket,
        )
        before_search = capture_opensearch_snapshot(
            phase10_opensearch_client,
            index_name=phase10_settings.opensearch_index,
            alias_name=phase10_settings.opensearch_alias,
        )

        delete_opensearch_targets(
            target_manifest,
            client=phase10_opensearch_client,
            checkpoint=lambda: None,
            current_index_name=phase10_settings.opensearch_index,
            current_index_alias=phase10_settings.opensearch_alias,
            timeout_seconds=30,
        )
        delete_minio_derived_targets(
            target_manifest,
            client=phase10_minio_client,
            checkpoint=lambda: None,
            heartbeat_interval_seconds=1,
            heartbeat_object_interval=25,
        )
        delete_minio_raw_target(
            target_manifest,
            client=phase10_minio_client,
            checkpoint=lambda: None,
            heartbeat_interval_seconds=1,
            heartbeat_object_interval=25,
        )
        after_minio = capture_minio_snapshot(
            phase10_minio_client,
            phase10_settings.minio_bucket,
        )
        after_search = capture_opensearch_snapshot(
            phase10_opensearch_client,
            index_name=phase10_settings.opensearch_index,
            alias_name=phase10_settings.opensearch_alias,
        )
        before = Phase10ResourceSnapshot(
            minio_versions=before_minio.minio_versions,
            opensearch_ids=before_search.opensearch_ids,
            opensearch_document_count=before_search.opensearch_document_count,
            opensearch_alias_targets=before_search.opensearch_alias_targets,
            opensearch_mapping_sha256=before_search.opensearch_mapping_sha256,
        )
        after = Phase10ResourceSnapshot(
            minio_versions=after_minio.minio_versions,
            opensearch_ids=after_search.opensearch_ids,
            opensearch_document_count=after_search.opensearch_document_count,
            opensearch_alias_targets=after_search.opensearch_alias_targets,
            opensearch_mapping_sha256=after_search.opensearch_mapping_sha256,
        )
        assert_unrelated_resources_unchanged(
            before,
            after,
            target_minio_keys=set(target.all_minio_keys),
            target_opensearch_ids={str(chunk_id) for chunk_id in target.chunk_ids},
        )
        assert not any(entry[0] in target.all_minio_keys for entry in after.minio_versions)
        assert not ({str(chunk_id) for chunk_id in target.chunk_ids} & set(after.opensearch_ids))
    finally:
        delete_opensearch_targets(
            control_manifest,
            client=phase10_opensearch_client,
            checkpoint=lambda: None,
            current_index_name=phase10_settings.opensearch_index,
            current_index_alias=phase10_settings.opensearch_alias,
            timeout_seconds=30,
        )
        delete_minio_derived_targets(
            control_manifest,
            client=phase10_minio_client,
            checkpoint=lambda: None,
            heartbeat_interval_seconds=1,
            heartbeat_object_interval=25,
        )
        delete_minio_raw_target(
            control_manifest,
            client=phase10_minio_client,
            checkpoint=lambda: None,
            heartbeat_interval_seconds=1,
            heartbeat_object_interval=25,
        )


@pytest.mark.parametrize("lose_response_after_delete", [False, True])
def test_opensearch_transport_failure_then_retry_converges_even_if_first_dbq_completed(
    phase10_settings,
    phase10_opensearch_client,
    phase10_document_factory,
    lose_response_after_delete: bool,
) -> None:
    target = phase10_document_factory.create()
    manifest = build_storage_manifest(
        target,
        phase10_settings,
        document_factory=phase10_document_factory,
    )
    index_opensearch_fixture(
        phase10_opensearch_client,
        target,
        phase10_settings.opensearch_index,
        document_factory=phase10_document_factory,
    )
    faulting = OneShotOpenSearchFault(
        phase10_opensearch_client,
        lose_response_after_delete=lose_response_after_delete,
    )

    with pytest.raises(DocumentDeletionStorageError):
        delete_opensearch_targets(
            manifest,
            client=faulting,
            checkpoint=lambda: None,
            current_index_name=phase10_settings.opensearch_index,
            current_index_alias=phase10_settings.opensearch_alias,
            timeout_seconds=30,
        )

    delete_opensearch_targets(
        manifest,
        client=faulting,
        checkpoint=lambda: None,
        current_index_name=phase10_settings.opensearch_index,
        current_index_alias=phase10_settings.opensearch_alias,
        timeout_seconds=30,
    )
    assert phase10_opensearch_client.count(
        index=phase10_settings.opensearch_index,
        body={"query": {"term": {"document_id": str(target.document_id)}}},
    )["count"] == 0


def test_long_minio_operation_heartbeats_beyond_initial_lease_and_blocks_second_claim(
    phase10_session_factory,
    phase10_settings,
    phase10_document_factory,
    phase10_runtime_settings,
) -> None:
    target = phase10_document_factory.create()
    other_pending = phase10_document_factory.create()
    other_retry = phase10_document_factory.create()
    manifest = build_storage_manifest(
        target,
        phase10_settings,
        document_factory=phase10_document_factory,
    )
    slow_keys = [
        f"{manifest.derived_prefixes[0]}images/slow-{index}.png"
        for index in range(12)
    ]
    minio = SlowFakeMinio(slow_keys, delay_seconds=0.3)
    with phase10_session_factory() as session:
        for identity in (target, other_pending, other_retry):
            persist_document_fixture(
                session,
                identity,
                bucket_name=phase10_settings.minio_bucket,
                document_factory=phase10_document_factory,
            )
        claimed = _persist_processing_heartbeat_target(
            session,
            target,
            phase10_settings,
            document_factory=phase10_document_factory,
            locked_by="heartbeat-owner",
            lease_seconds=3,
        )
        pending_job = schedule_pending_job(
            session,
            other_pending,
            phase10_settings,
            current_step="delete_minio_derived",
            document_factory=phase10_document_factory,
        )
        retry_job = schedule_pending_job(
            session,
            other_retry,
            phase10_settings,
            current_step="delete_minio_derived",
            document_factory=phase10_document_factory,
        )
        retry_job.status = "retry_wait"
        retry_job.next_retry_at = func.now() - text("INTERVAL '1 second'")
        session.commit()
        initial_expiry = session.get(
            DocumentDeletionJob, claimed.job_id
        ).lease_expires_at
        initial_token = claimed.lease_token
        existing_job_count = int(
            session.scalar(select(func.count()).select_from(DocumentDeletionJob)) or 0
        )

    renewals: list[bool] = []
    unrelated_claimed_ids: set[UUID] = set()

    def checkpoint() -> None:
        with phase10_session_factory() as heartbeat_session:
            renewals.append(
                renew_claimed_deletion(heartbeat_session, claimed, lease_seconds=3)
            )
            heartbeat_session.commit()
        assert renewals[-1] is True

    with ThreadPoolExecutor(max_workers=1) as pool:
        deletion = pool.submit(
            delete_minio_derived_targets,
            manifest,
            client=minio,
            checkpoint=checkpoint,
            heartbeat_interval_seconds=1,
            heartbeat_object_interval=25,
            delete_batch_size=5,
        )
        while not deletion.done():
            time.sleep(0.25)
            with phase10_session_factory() as contender_session:
                drained = drain_claimable_jobs_excluding_target(
                    contender_session,
                    target_job_id=claimed.job_id,
                    max_claims=existing_job_count + 2,
                )
            unrelated_claimed_ids.update(drained)
        deletion.result(timeout=1)

    with phase10_session_factory() as session:
        target_job = session.get(DocumentDeletionJob, claimed.job_id)
        restored_pending = session.get(DocumentDeletionJob, pending_job.id)
        restored_retry = session.get(DocumentDeletionJob, retry_job.id)
        renewed_expiry = target_job.lease_expires_at
        assert target_job.status == "processing"
        assert target_job.locked_by == "heartbeat-owner"
        assert target_job.lease_token == initial_token
        assert session.scalar(
            select(DocumentDeletionJob.lease_expires_at > func.now()).where(
                DocumentDeletionJob.id == claimed.job_id
            )
        ) is True
        assert restored_pending.status == "pending"
        assert restored_retry.status == "retry_wait"

    assert renewed_expiry > initial_expiry
    assert len(renewals) >= 3
    assert minio.remove_calls == 3
    assert {pending_job.id, retry_job.id}.issubset(unrelated_claimed_ids)
    assert claimed.job_id not in unrelated_claimed_ids


def test_lease_lost_checkpoint_stops_slow_minio_before_new_external_operation(
    phase10_session_factory,
    phase10_settings,
    phase10_document_factory,
) -> None:
    target = phase10_document_factory.create()
    manifest = build_storage_manifest(
        target,
        phase10_settings,
        document_factory=phase10_document_factory,
    )
    minio = SlowFakeMinio(
        [f"{manifest.derived_prefixes[0]}images/not-started.png"],
        delay_seconds=0,
    )
    with phase10_session_factory() as session:
        persist_document_fixture(
            session,
            target,
            bucket_name=phase10_settings.minio_bucket,
            document_factory=phase10_document_factory,
        )
        schedule_pending_job(
            session,
            target,
            phase10_settings,
            current_step="delete_minio_derived",
            document_factory=phase10_document_factory,
        )
        session.commit()
        old_claim = claim_document_deletion(
            session,
            locked_by="old-owner",
            lease_token=uuid4(),
            lease_seconds=5,
        )
        session.commit()
    assert old_claim is not None

    with phase10_session_factory() as session:
        session.execute(
            update(DocumentDeletionJob)
            .where(DocumentDeletionJob.id == old_claim.job_id)
            .values(lease_expires_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'"))
        )
        session.commit()
        assert claim_document_deletion(
            session,
            locked_by="new-owner",
            lease_token=uuid4(),
            lease_seconds=5,
        ) is not None
        session.commit()

    def stale_checkpoint() -> None:
        with phase10_session_factory() as session:
            owned = renew_claimed_deletion(session, old_claim, lease_seconds=5)
            session.rollback()
        if not owned:
            raise DocumentDeletionLeaseLost()

    with pytest.raises(DocumentDeletionLeaseLost):
        delete_minio_derived_targets(
            manifest,
            client=minio,
            checkpoint=stale_checkpoint,
            heartbeat_interval_seconds=1,
            heartbeat_object_interval=25,
        )

    assert minio.list_calls == 0
    assert minio.remove_calls == 0


def test_stale_heartbeat_cannot_resurrect_old_owner(
    phase10_session_factory,
    phase10_settings,
    phase10_document_factory,
) -> None:
    target = phase10_document_factory.create()
    with phase10_session_factory() as session:
        persist_document_fixture(
            session,
            target,
            bucket_name=phase10_settings.minio_bucket,
            document_factory=phase10_document_factory,
        )
        schedule_pending_job(
            session,
            target,
            phase10_settings,
            current_step="delete_minio_derived",
            document_factory=phase10_document_factory,
        )
        session.commit()
        old_claim = claim_document_deletion(
            session,
            locked_by="old-owner",
            lease_token=uuid4(),
            lease_seconds=5,
        )
        session.commit()
    assert old_claim is not None

    with phase10_session_factory() as session:
        session.execute(
            update(DocumentDeletionJob)
            .where(DocumentDeletionJob.id == old_claim.job_id)
            .values(lease_expires_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'"))
        )
        session.commit()
        new_claim = claim_document_deletion(
            session,
            locked_by="new-owner",
            lease_token=uuid4(),
            lease_seconds=5,
        )
        session.commit()
    assert new_claim is not None

    with phase10_session_factory() as session:
        assert renew_claimed_deletion(session, old_claim, lease_seconds=5) is False
        session.rollback()


def test_real_minio_partial_delete_retries_only_remaining_versions(
    phase10_settings,
    phase10_minio_client,
    phase10_document_factory,
) -> None:
    target = phase10_document_factory.create()
    manifest = build_storage_manifest(
        target,
        phase10_settings,
        document_factory=phase10_document_factory,
    )
    upload_minio_fixture_versions(
        phase10_minio_client,
        target,
        phase10_settings.minio_bucket,
        document_factory=phase10_document_factory,
    )
    derived_keys = set(target.explicit_derived_keys)
    before = {
        entry
        for entry in capture_minio_snapshot(
            phase10_minio_client,
            phase10_settings.minio_bucket,
        ).minio_versions
        if entry[0] in derived_keys
    }
    assert len(before) >= 3

    faulting = OneShotPartialDeleteMinioClient(
        phase10_minio_client,
        successful_prefix_size=1,
    )
    with pytest.raises(DocumentDeletionStorageError) as failure:
        delete_minio_derived_targets(
            manifest,
            client=faulting,
            checkpoint=lambda: None,
            heartbeat_interval_seconds=1,
            delete_batch_size=1000,
        )
    assert failure.value.code == DOCUMENT_DELETION_MINIO_DELETE_FAILED

    after_first = {
        entry
        for entry in capture_minio_snapshot(
            phase10_minio_client,
            phase10_settings.minio_bucket,
        ).minio_versions
        if entry[0] in derived_keys
    }
    assert len(after_first) == len(before) - 1
    assert len(faulting.first_attempt_deleted) == 1
    deleted_version = faulting.first_attempt_deleted[0]
    assert not any(entry[:2] == deleted_version for entry in after_first)

    delete_minio_derived_targets(
        manifest,
        client=faulting,
        checkpoint=lambda: None,
        heartbeat_interval_seconds=1,
        delete_batch_size=1000,
    )

    after_retry = {
        entry
        for entry in capture_minio_snapshot(
            phase10_minio_client,
            phase10_settings.minio_bucket,
        ).minio_versions
        if entry[0] in derived_keys
    }
    assert after_retry == set()
    assert deleted_version not in faulting.retry_attempted
    assert set(faulting.retry_attempted) == {entry[:2] for entry in after_first}


def test_real_minio_raw_absence_is_idempotent_success(
    phase10_settings,
    phase10_minio_client,
    phase10_document_factory,
) -> None:
    target = phase10_document_factory.create()
    manifest = build_storage_manifest(
        target,
        phase10_settings,
        document_factory=phase10_document_factory,
    )
    assert not any(
        entry[0] == target.raw_object_key
        for entry in capture_minio_snapshot(
            phase10_minio_client,
            phase10_settings.minio_bucket,
        ).minio_versions
    )

    delete_minio_raw_target(
        manifest,
        client=phase10_minio_client,
        checkpoint=lambda: None,
        heartbeat_interval_seconds=1,
    )

    assert not any(
        entry[0] == target.raw_object_key
        for entry in capture_minio_snapshot(
            phase10_minio_client,
            phase10_settings.minio_bucket,
        ).minio_versions
    )


def test_real_minio_missing_run_bucket_is_failure(
    phase10_run_context,
    phase10_settings,
    phase10_minio_client,
    phase10_document_factory,
) -> None:
    phase10_run_context.validate_storage_extension_resources(
        minio_locked_bucket=phase10_run_context.minio_locked_bucket,
        minio_missing_bucket=phase10_run_context.minio_missing_bucket,
        opensearch_rollover_index=phase10_run_context.opensearch_rollover_index,
    )
    assert not phase10_minio_client.bucket_exists(
        phase10_run_context.minio_missing_bucket
    )
    target = phase10_document_factory.create()
    manifest = replace(
        build_storage_manifest(
            target,
            phase10_settings,
            document_factory=phase10_document_factory,
        ),
        bucket_name=phase10_run_context.minio_missing_bucket,
    )

    with pytest.raises(DocumentDeletionStorageError) as failure:
        delete_minio_raw_target(
            manifest,
            client=phase10_minio_client,
            checkpoint=lambda: None,
            heartbeat_interval_seconds=1,
        )

    assert failure.value.code == DOCUMENT_DELETION_MINIO_BUCKET_NOT_FOUND


def test_real_minio_compliance_retention_prevents_hard_delete(
    phase10_run_context,
    phase10_settings,
    phase10_minio_client,
    phase10_document_factory,
) -> None:
    locked_bucket = phase10_run_context.minio_locked_bucket
    phase10_run_context.validate_storage_extension_resources(
        minio_locked_bucket=locked_bucket,
        minio_missing_bucket=phase10_run_context.minio_missing_bucket,
        opensearch_rollover_index=phase10_run_context.opensearch_rollover_index,
    )
    assert not phase10_minio_client.bucket_exists(locked_bucket)
    phase10_minio_client.make_bucket(locked_bucket, object_lock=True)
    versioning = phase10_minio_client.get_bucket_versioning(locked_bucket)
    assert str(getattr(versioning, "status", "")).lower() == "enabled"

    target = phase10_document_factory.create()
    payload = b"phase10-retained-version"
    uploaded = phase10_minio_client.put_object(
        locked_bucket,
        target.raw_object_key,
        BytesIO(payload),
        len(payload),
        content_type="application/pdf",
    )
    assert uploaded.version_id
    retain_until = datetime.now(UTC) + timedelta(minutes=15)
    phase10_minio_client.set_object_retention(
        locked_bucket,
        target.raw_object_key,
        Retention(COMPLIANCE, retain_until),
        version_id=uploaded.version_id,
    )
    manifest = replace(
        build_storage_manifest(
            target,
            phase10_settings,
            document_factory=phase10_document_factory,
        ),
        bucket_name=locked_bucket,
    )

    with pytest.raises(DocumentDeletionStorageError) as failure:
        delete_minio_raw_target(
            manifest,
            client=phase10_minio_client,
            checkpoint=lambda: None,
            heartbeat_interval_seconds=1,
        )

    assert failure.value.code == DOCUMENT_DELETION_MINIO_DELETE_FAILED
    retained = {
        entry
        for entry in capture_minio_snapshot(
            phase10_minio_client,
            locked_bucket,
        ).minio_versions
        if entry[0] == target.raw_object_key
    }
    assert (target.raw_object_key, uploaded.version_id, False) in retained


def test_real_opensearch_dbq_failure_records_retry_wait_then_advances(
    phase10_settings,
    phase10_session_factory,
    phase10_minio_client,
    phase10_opensearch_client,
    phase10_document_factory,
    phase10_runtime_settings,
) -> None:
    target = phase10_document_factory.create()
    with phase10_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DocumentDeletionJob)) == 0
        persist_document_fixture(
            session,
            target,
            bucket_name=phase10_settings.minio_bucket,
            document_factory=phase10_document_factory,
        )
        job = schedule_pending_job(
            session,
            target,
            phase10_runtime_settings,
            current_step="delete_opensearch",
            document_factory=phase10_document_factory,
        )
        session.commit()
        job_id = job.id
    index_opensearch_fixture(
        phase10_opensearch_client,
        target,
        phase10_settings.opensearch_index,
        document_factory=phase10_document_factory,
    )
    faulting = OneShotOpenSearchFault(
        phase10_opensearch_client,
        lose_response_after_delete=False,
    )

    def advance_step(claimed, next_step: str) -> None:
        with phase10_session_factory() as session:
            assert advance_claimed_deletion(
                session,
                claimed,
                next_step=next_step,
            )
            session.commit()

    saga = DocumentDeletionSaga(
        settings=phase10_runtime_settings,
        minio_client=phase10_minio_client,
        opensearch_client=faulting,
        advance_step=advance_step,
        finalize_step=lambda *_: pytest.fail("Unexpected finalization step."),
    )
    executor = DocumentDeletionExecutor(
        session_factory=phase10_session_factory,
        settings=phase10_runtime_settings,
        worker_id=f"phase10-storage-coverage-{target.document_id}",
        saga=saga,
    )

    assert executor.run_once() is True
    with phase10_session_factory() as session:
        failed_job = session.get(DocumentDeletionJob, job_id)
        document = session.get(Document, target.document_id)
        assert failed_job is not None
        assert failed_job.status == "retry_wait"
        assert failed_job.current_step == "delete_opensearch"
        assert failed_job.step_attempts == 1
        assert failed_job.next_retry_at is not None
        assert failed_job.last_error_code == DOCUMENT_DELETION_OPENSEARCH_UNAVAILABLE
        assert document is not None and document.deletion_status == "deleting"
        assert session.scalar(
            select(DocumentDeletionJob.next_retry_at > func.now()).where(
                DocumentDeletionJob.id == job_id
            )
        ) is True
        session.execute(
            update(DocumentDeletionJob)
            .where(DocumentDeletionJob.id == job_id)
            .values(next_retry_at=func.now() - text("INTERVAL '1 second'"))
        )
        session.commit()

    assert executor.run_once() is True
    with phase10_session_factory() as session:
        advanced_job = session.get(DocumentDeletionJob, job_id)
        assert advanced_job is not None
        assert advanced_job.status == "pending"
        assert advanced_job.current_step == "delete_minio_derived"
        assert advanced_job.step_attempts == 0
        assert advanced_job.next_retry_at is None
        assert advanced_job.last_error_code is None
    assert phase10_opensearch_client.count(
        index=phase10_settings.opensearch_index,
        body={"query": {"term": {"document_id": str(target.document_id)}}},
    )["count"] == 0


def test_real_opensearch_rollover_deletes_manifest_and_current_targets(
    phase10_run_context,
    phase10_settings,
    phase10_opensearch_client,
    phase10_document_factory,
    phase10_runtime_settings,
) -> None:
    rollover_index = phase10_run_context.opensearch_rollover_index
    phase10_run_context.validate_storage_extension_resources(
        minio_locked_bucket=phase10_run_context.minio_locked_bucket,
        minio_missing_bucket=phase10_run_context.minio_missing_bucket,
        opensearch_rollover_index=rollover_index,
    )
    assert not phase10_opensearch_client.indices.exists(index=rollover_index)
    rollover_body = deepcopy(build_casting_chunks_index_body(phase10_runtime_settings))
    rollover_body["aliases"] = {}
    phase10_opensearch_client.indices.create(
        index=rollover_index,
        body=rollover_body,
    )

    target = phase10_document_factory.create()
    control = phase10_document_factory.create()
    manifest = build_storage_manifest(
        target,
        phase10_settings,
        document_factory=phase10_document_factory,
    )
    for index_name in (phase10_settings.opensearch_index, rollover_index):
        index_opensearch_fixture(
            phase10_opensearch_client,
            target,
            index_name,
            document_factory=phase10_document_factory,
        )
        index_opensearch_fixture(
            phase10_opensearch_client,
            control,
            index_name,
            document_factory=phase10_document_factory,
        )
    mapping_before = {
        index_name: stable_json_sha256(
            phase10_opensearch_client.indices.get_mapping(index=index_name)[
                index_name
            ]["mappings"]
        )
        for index_name in (phase10_settings.opensearch_index, rollover_index)
    }
    assert all(
        phase10_opensearch_client.count(
            index=index_name,
            body={"query": {"term": {"document_id": str(target.document_id)}}},
        )["count"]
        > 0
        for index_name in (phase10_settings.opensearch_index, rollover_index)
    )

    phase10_opensearch_client.indices.update_aliases(
        body={
            "actions": [
                {
                    "remove": {
                        "index": phase10_settings.opensearch_index,
                        "alias": phase10_settings.opensearch_alias,
                    }
                },
                {
                    "add": {
                        "index": rollover_index,
                        "alias": phase10_settings.opensearch_alias,
                    }
                },
            ]
        }
    )
    assert tuple(
        sorted(
            phase10_opensearch_client.indices.get_alias(
                name=phase10_settings.opensearch_alias
            )
        )
    ) == (rollover_index,)

    delete_opensearch_targets(
        manifest,
        client=phase10_opensearch_client,
        checkpoint=lambda: None,
        current_index_name=rollover_index,
        current_index_alias=phase10_settings.opensearch_alias,
        timeout_seconds=30,
    )

    for index_name in (phase10_settings.opensearch_index, rollover_index):
        assert phase10_opensearch_client.count(
            index=index_name,
            body={"query": {"term": {"document_id": str(target.document_id)}}},
        )["count"] == 0
        assert phase10_opensearch_client.count(
            index=index_name,
            body={"query": {"term": {"document_id": str(control.document_id)}}},
        )["count"] == len(control.chunk_ids)
        assert stable_json_sha256(
            phase10_opensearch_client.indices.get_mapping(index=index_name)[
                index_name
            ]["mappings"]
        ) == mapping_before[index_name]
    assert tuple(
        sorted(
            phase10_opensearch_client.indices.get_alias(
                name=phase10_settings.opensearch_alias
            )
        )
    ) == (rollover_index,)
