from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update

import pytest

from app.models.document_deletion_job import DocumentDeletionJob
from app.services.document_deletion import (
    ClaimedDocumentDeletion,
    claim_document_deletion,
    renew_claimed_deletion,
)
from app.services.document_deletion_storage import (
    DocumentDeletionLeaseLost,
    DocumentDeletionStorageError,
)
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
)


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
                (delete_object.object_name, delete_object.version_id),
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
    locked_by: str,
    lease_seconds: int,
) -> ClaimedDocumentDeletion:
    job = schedule_pending_job(
        session,
        identity,
        settings,
        current_step="delete_minio_derived",
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
            )
        claimed = _persist_processing_heartbeat_target(
            session,
            target,
            phase10_settings,
            locked_by="fixture-heartbeat-owner",
            lease_seconds=30,
        )
        pending_job = schedule_pending_job(
            session,
            other_pending,
            phase10_settings,
            current_step="delete_minio_derived",
        )
        retry_job = schedule_pending_job(
            session,
            other_retry,
            phase10_settings,
            current_step="delete_minio_derived",
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
    target_manifest = build_storage_manifest(target, phase10_settings)
    control_manifest = build_storage_manifest(control, phase10_settings)
    upload_minio_fixture_versions(phase10_minio_client, target, phase10_settings.minio_bucket)
    upload_minio_fixture_versions(phase10_minio_client, control, phase10_settings.minio_bucket)
    index_opensearch_fixture(phase10_opensearch_client, target, phase10_settings.opensearch_index)
    index_opensearch_fixture(phase10_opensearch_client, control, phase10_settings.opensearch_index)

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
    manifest = build_storage_manifest(target, phase10_settings)
    index_opensearch_fixture(
        phase10_opensearch_client,
        target,
        phase10_settings.opensearch_index,
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
    manifest = build_storage_manifest(target, phase10_settings)
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
            )
        claimed = _persist_processing_heartbeat_target(
            session,
            target,
            phase10_settings,
            locked_by="heartbeat-owner",
            lease_seconds=3,
        )
        pending_job = schedule_pending_job(
            session,
            other_pending,
            phase10_settings,
            current_step="delete_minio_derived",
        )
        retry_job = schedule_pending_job(
            session,
            other_retry,
            phase10_settings,
            current_step="delete_minio_derived",
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
    manifest = build_storage_manifest(target, phase10_settings)
    minio = SlowFakeMinio(
        [f"{manifest.derived_prefixes[0]}images/not-started.png"],
        delay_seconds=0,
    )
    with phase10_session_factory() as session:
        persist_document_fixture(
            session,
            target,
            bucket_name=phase10_settings.minio_bucket,
        )
        schedule_pending_job(
            session,
            target,
            phase10_settings,
            current_step="delete_minio_derived",
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
        )
        schedule_pending_job(session, target, phase10_settings, current_step="delete_minio_derived")
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
