from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from sqlalchemy import select, text, update

import pytest

from app.models.document_deletion_job import DocumentDeletionJob
from app.models.knowledge_item import KnowledgeItem
from app.services.document_deletion import (
    advance_claimed_deletion,
    claim_document_deletion,
    finalize_postgresql_deletion,
    renew_claimed_deletion,
    sweep_exhausted_deletions,
)
from integration.phase10_fixtures import (
    build_finalization_claim,
    persist_document_fixture,
    persist_revision_boundary_fixture,
    persist_revision_cycle_fixture,
    persist_shared_knowledge_fixture,
    schedule_pending_job,
)


pytestmark = pytest.mark.integration


def _run_concurrently(*operations):
    barrier = Barrier(len(operations))

    def invoke(operation):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(invoke, operation) for operation in operations]
        return [future.result(timeout=30) for future in futures]


def test_two_real_transactions_claim_one_job_once(
    phase10_session_factory,
    phase10_document_factory,
    phase10_runtime_settings,
) -> None:
    target = phase10_document_factory.create()
    with phase10_session_factory() as session:
        persist_document_fixture(
            session,
            target,
            bucket_name=phase10_runtime_settings.minio_bucket,
        )
        schedule_pending_job(
            session,
            target,
            phase10_runtime_settings,
            current_step="delete_opensearch",
        )
        session.commit()

    def claimant(worker: str):
        with phase10_session_factory() as session:
            claimed = claim_document_deletion(
                session,
                locked_by=worker,
                lease_token=uuid4(),
                lease_seconds=3,
            )
            session.commit()
            return claimed

    results = _run_concurrently(lambda: claimant("worker-a"), lambda: claimant("worker-b"))
    assert sum(result is not None for result in results) == 1


def test_expiry_fencing_heartbeat_and_exhaustion_are_mutually_exclusive(
    phase10_session_factory,
    phase10_document_factory,
    phase10_runtime_settings,
) -> None:
    target = phase10_document_factory.create()
    with phase10_session_factory() as session:
        persist_document_fixture(
            session,
            target,
            bucket_name=phase10_runtime_settings.minio_bucket,
        )
        old_claim, _manifest = build_finalization_claim(
            session,
            target,
            knowledge=None,
            settings=phase10_runtime_settings,
            current_step="delete_opensearch",
        )
        session.commit()
        session.execute(
            update(DocumentDeletionJob)
            .where(DocumentDeletionJob.id == old_claim.job_id)
            .values(lease_expires_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'"))
        )
        session.commit()

    with phase10_session_factory() as session:
        new_claim = claim_document_deletion(
            session,
            locked_by="new-owner",
            lease_token=uuid4(),
            lease_seconds=5,
        )
        session.commit()
    assert new_claim is not None

    with phase10_session_factory() as session:
        assert advance_claimed_deletion(
            session,
            old_claim,
            next_step="delete_minio_derived",
        ) is False
        assert renew_claimed_deletion(session, new_claim, lease_seconds=5) is True
        session.commit()

    with phase10_session_factory() as session:
        assert (
            claim_document_deletion(
                session,
                locked_by="third-owner",
                lease_token=uuid4(),
                lease_seconds=5,
            )
            is None
        )
        session.rollback()

        session.execute(
            update(DocumentDeletionJob)
            .where(DocumentDeletionJob.id == new_claim.job_id)
            .values(
                step_attempts=DocumentDeletionJob.max_attempts,
                lease_expires_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'"),
            )
        )
        session.commit()

    with phase10_session_factory() as session:
        assert (
            claim_document_deletion(
                session,
                locked_by="forbidden-extra-attempt",
                lease_token=uuid4(),
                lease_seconds=5,
            )
            is None
        )
        assert sweep_exhausted_deletions(session) == (new_claim.job_id,)
        session.commit()


def test_simultaneous_a_b_finalization_deletes_last_shared_source_without_deadlock(
    phase10_session_factory,
    phase10_document_factory,
    phase10_runtime_settings,
) -> None:
    document_a = phase10_document_factory.create()
    document_b = phase10_document_factory.create()
    with phase10_session_factory() as session:
        persist_document_fixture(
            session,
            document_a,
            bucket_name=phase10_runtime_settings.minio_bucket,
        )
        persist_document_fixture(
            session,
            document_b,
            bucket_name=phase10_runtime_settings.minio_bucket,
        )
        knowledge = persist_shared_knowledge_fixture(
            session,
            target=document_a,
            control=document_b,
        )
        claim_a, manifest_a = build_finalization_claim(
            session,
            document_a,
            knowledge=knowledge,
            settings=phase10_runtime_settings,
        )
        claim_b, manifest_b = build_finalization_claim(
            session,
            document_b,
            knowledge=knowledge,
            settings=phase10_runtime_settings,
        )
        session.commit()

    def finalize(claimed, manifest):
        with phase10_session_factory() as session:
            finalize_postgresql_deletion(session, claimed=claimed, manifest=manifest)
            session.commit()

    _run_concurrently(
        lambda: finalize(claim_a, manifest_a),
        lambda: finalize(claim_b, manifest_b),
    )

    with phase10_session_factory() as session:
        assert session.get(KnowledgeItem, knowledge.shared_item_id) is None
        assert session.scalar(
            select(DocumentDeletionJob).where(
                DocumentDeletionJob.id.in_((claim_a.job_id, claim_b.job_id))
            )
        ) is None


def test_overlapping_revision_cycle_concurrent_finalization_terminates_without_deadlock(
    phase10_session_factory,
    phase10_document_factory,
    phase10_runtime_settings,
) -> None:
    document_a = phase10_document_factory.create()
    document_b = phase10_document_factory.create()
    with phase10_session_factory() as session:
        persist_document_fixture(
            session,
            document_a,
            bucket_name=phase10_runtime_settings.minio_bucket,
        )
        persist_document_fixture(
            session,
            document_b,
            bucket_name=phase10_runtime_settings.minio_bucket,
        )
        revision = persist_revision_cycle_fixture(
            session,
            document_a=document_a,
            document_b=document_b,
        )
        claim_a, manifest_a = build_finalization_claim(
            session,
            document_a,
            knowledge=revision,
            settings=phase10_runtime_settings,
        )
        claim_b, manifest_b = build_finalization_claim(
            session,
            document_b,
            knowledge=revision,
            settings=phase10_runtime_settings,
        )
        session.commit()

    def finalize(claimed, manifest):
        with phase10_session_factory() as session:
            finalize_postgresql_deletion(session, claimed=claimed, manifest=manifest)
            session.commit()

    _run_concurrently(
        lambda: finalize(claim_a, manifest_a),
        lambda: finalize(claim_b, manifest_b),
    )

    with phase10_session_factory() as session:
        assert all(session.get(KnowledgeItem, item_id) is None for item_id in revision.all_item_ids)


def test_real_self_cycle_terminates_and_surviving_revision_detaches_from_orphan_parent(
    phase10_session_factory,
    phase10_document_factory,
    phase10_runtime_settings,
) -> None:
    document_a = phase10_document_factory.create()
    document_b = phase10_document_factory.create()
    with phase10_session_factory() as session:
        persist_document_fixture(
            session,
            document_a,
            bucket_name=phase10_runtime_settings.minio_bucket,
        )
        persist_document_fixture(
            session,
            document_b,
            bucket_name=phase10_runtime_settings.minio_bucket,
        )
        boundary = persist_revision_boundary_fixture(
            session,
            document_a=document_a,
            document_b=document_b,
        )
        claimed, manifest = build_finalization_claim(
            session,
            document_a,
            knowledge=None,
            settings=phase10_runtime_settings,
        )
        session.commit()

    with phase10_session_factory() as session:
        finalize_postgresql_deletion(session, claimed=claimed, manifest=manifest)
        session.commit()

    with phase10_session_factory() as session:
        assert session.get(KnowledgeItem, boundary.orphan_parent_id) is None
        assert session.get(KnowledgeItem, boundary.self_cycle_id) is None
        surviving = session.get(KnowledgeItem, boundary.surviving_child_id)
        assert surviving is not None
        assert surviving.revises_item_id is None
