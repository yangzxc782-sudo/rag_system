from __future__ import annotations

from threading import Event
from types import SimpleNamespace
from uuid import UUID

from app.services.document_deletion import ClaimedDocumentDeletion
from app.services.document_deletion_manifest import DocumentDeletionManifest
from app.services.document_deletion_storage import DocumentDeletionLeaseLost
from app.tasks.document_deletion_executor import DocumentDeletionExecutor


JOB_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DOCUMENT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
LEASE_TOKEN = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def claimed_job(step: str = "delete_opensearch") -> ClaimedDocumentDeletion:
    return ClaimedDocumentDeletion(
        job_id=JOB_ID,
        document_id=DOCUMENT_ID,
        current_step=step,
        step_attempts=1,
        max_attempts=5,
        manifest={"schema_version": 1},
        lease_token=LEASE_TOKEN,
    )


def executor_settings(*, enabled: bool = True, poll: float = 0.01) -> SimpleNamespace:
    return SimpleNamespace(
        document_deletion_executor_enabled=enabled,
        document_deletion_poll_interval_seconds=poll,
        document_deletion_shutdown_grace_seconds=1,
        document_deletion_lease_seconds=120,
        document_deletion_retry_base_seconds=5,
        document_deletion_retry_max_seconds=300,
    )


class FakeSession:
    def __init__(self, owner: "SessionFactory") -> None:
        self.owner = owner
        self.closed = False
        self.owner.active += 1
        self.owner.created.append(self)

    def commit(self) -> None:
        self.owner.commits += 1
        if self.owner.commits == self.owner.fail_commit_number:
            raise RuntimeError("controlled-commit-failure")

    def rollback(self) -> None:
        self.owner.rollbacks += 1

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.owner.active -= 1


class SessionFactory:
    def __init__(self, *, fail_commit_number: int | None = None) -> None:
        self.created: list[FakeSession] = []
        self.active = 0
        self.commits = 0
        self.rollbacks = 0
        self.fail_commit_number = fail_commit_number

    def __call__(self) -> FakeSession:
        return FakeSession(self)


class RecordingSaga:
    def __init__(self, *, session_factory: SessionFactory, checkpoint_calls: int = 1) -> None:
        self.session_factory = session_factory
        self.checkpoint_calls = checkpoint_calls
        self.calls = 0

    def run_step(self, _claimed: ClaimedDocumentDeletion, *, checkpoint: object) -> None:
        self.calls += 1
        assert self.session_factory.active == 0
        for _ in range(self.checkpoint_calls):
            checkpoint()
            assert self.session_factory.active == 0


def build_executor(
    *,
    session_factory: SessionFactory,
    saga: object,
    enabled: bool = True,
    claim_results: list[ClaimedDocumentDeletion | None] | None = None,
    renew_result: bool = True,
    sweep_result: tuple[UUID, ...] = (),
) -> tuple[DocumentDeletionExecutor, dict[str, object]]:
    claims = list(claim_results or [])
    calls: dict[str, object] = {
        "sweep": 0,
        "claim": 0,
        "renew": 0,
        "advances": [],
        "finalizations": [],
        "failures": [],
    }

    def sweep(_db: object) -> tuple[UUID, ...]:
        calls["sweep"] = int(calls["sweep"]) + 1
        return sweep_result

    def claim(_db: object, **_kwargs: object) -> ClaimedDocumentDeletion | None:
        calls["claim"] = int(calls["claim"]) + 1
        return claims.pop(0) if claims else None

    def renew(_db: object, _claimed: ClaimedDocumentDeletion, **_kwargs: object) -> bool:
        calls["renew"] = int(calls["renew"]) + 1
        return renew_result

    def fail(
        _db: object,
        _claimed: ClaimedDocumentDeletion,
        *,
        retry_seconds: int,
        error_code: str,
    ) -> bool:
        calls["failures"].append((retry_seconds, error_code))
        return True

    def advance(
        _db: object,
        _claimed: ClaimedDocumentDeletion,
        *,
        next_step: str,
    ) -> bool:
        calls["advances"].append(next_step)
        return True

    def finalize(
        _db: object,
        *,
        claimed: ClaimedDocumentDeletion,
        manifest: DocumentDeletionManifest,
    ) -> None:
        calls["finalizations"].append((claimed.job_id, manifest.document_id))

    executor = DocumentDeletionExecutor(
        session_factory=session_factory,
        settings=executor_settings(enabled=enabled),
        saga=saga,
        sweep_operation=sweep,
        claim_operation=claim,
        renew_operation=renew,
        advance_operation=advance,
        failure_operation=fail,
        finalization_operation=finalize,
    )
    return executor, calls


def test_disabled_executor_never_starts_thread_or_opens_session() -> None:
    sessions = SessionFactory()
    saga = RecordingSaga(session_factory=sessions)
    executor, calls = build_executor(
        session_factory=sessions,
        saga=saga,
        enabled=False,
        claim_results=[claimed_job()],
    )

    executor.start()
    executor.wake()
    executor.stop()
    executor.join()

    assert executor.is_alive is False
    assert sessions.created == []
    assert calls["claim"] == 0
    assert saga.calls == 0


def test_run_once_sweeps_then_claims_and_every_operation_owns_fresh_closed_session() -> None:
    sessions = SessionFactory()
    saga = RecordingSaga(session_factory=sessions, checkpoint_calls=2)
    executor, calls = build_executor(
        session_factory=sessions,
        saga=saga,
        claim_results=[claimed_job()],
    )

    assert executor.run_once() is True

    assert calls["sweep"] == 1
    assert calls["claim"] == 1
    assert calls["renew"] == 2
    assert len(sessions.created) == 4
    assert all(session.closed for session in sessions.created)
    assert sessions.active == 0
    assert sessions.commits == 4


def test_lease_lost_does_not_record_ordinary_failure() -> None:
    sessions = SessionFactory()
    saga = RecordingSaga(session_factory=sessions)
    executor, calls = build_executor(
        session_factory=sessions,
        saga=saga,
        claim_results=[claimed_job()],
        renew_result=False,
    )

    assert executor.run_once() is True

    assert calls["renew"] == 1
    assert calls["failures"] == []


def test_step_failure_uses_current_attempt_backoff_in_independent_session() -> None:
    sessions = SessionFactory()

    class FailingSaga:
        def run_step(self, _claimed: object, *, checkpoint: object) -> None:
            del checkpoint
            assert sessions.active == 0
            raise RuntimeError("controlled-step-failure")

    executor, calls = build_executor(
        session_factory=sessions,
        saga=FailingSaga(),
        claim_results=[claimed_job()],
    )

    assert executor.run_once() is True

    assert calls["failures"] == [(5, "DOCUMENT_DELETION_STEP_FAILED")]
    assert len(sessions.created) == 3
    assert all(session.closed for session in sessions.created)


def test_progress_and_finalization_each_use_one_independent_closed_session() -> None:
    sessions = SessionFactory()
    saga = RecordingSaga(session_factory=sessions)
    executor, calls = build_executor(session_factory=sessions, saga=saga)
    deletion_manifest = DocumentDeletionManifest(
        schema_version=1,
        document_id=DOCUMENT_ID,
        bucket_name="rag-documents",
        raw_object_key=f"raw/2026/08/{DOCUMENT_ID}.pdf",
        derived_object_keys=(),
        derived_prefixes=(),
        parse_run_ids=(),
        block_ids=(),
        asset_ids=(),
        chunk_ids=(),
        knowledge_source_relation_ids=(),
        knowledge_item_ids=(),
        search_index_name="casting_chunks_v1",
        search_index_alias="casting_chunks_current",
    )

    executor._advance(claimed_job(), "delete_minio_derived")
    assert sessions.active == 0
    executor._finalize(claimed_job("finalize_postgresql"), deletion_manifest)

    assert calls["advances"] == ["delete_minio_derived"]
    assert calls["finalizations"] == [(JOB_ID, DOCUMENT_ID)]
    assert len(sessions.created) == 2
    assert sessions.commits == 2
    assert all(session.closed for session in sessions.created)


def test_start_wake_stop_and_join_are_idempotent_without_busy_loop() -> None:
    sessions = SessionFactory()
    saga = RecordingSaga(session_factory=sessions)
    executor, calls = build_executor(
        session_factory=sessions,
        saga=saga,
        claim_results=[],
    )

    executor.start()
    executor.start()
    executor.wake()
    executor.wake()
    executor.stop()
    executor.stop()
    executor.join()
    executor.join()

    assert executor.is_alive is False
    assert int(calls["claim"]) <= 1
    assert int(calls["sweep"]) <= 1


def test_inflight_external_success_after_stop_makes_no_progress_or_failure_transition() -> None:
    sessions = SessionFactory()
    external_started = Event()
    external_may_return = Event()
    transitions: list[str] = []

    class BlockingSaga:
        def run_step(self, _claimed: object, *, checkpoint: object) -> None:
            assert sessions.active == 0
            checkpoint()
            external_started.set()
            external_may_return.wait(timeout=1)
            checkpoint()
            transitions.append("progress")

    executor, calls = build_executor(
        session_factory=sessions,
        saga=BlockingSaga(),
        claim_results=[claimed_job(), None],
    )
    executor.start()
    assert external_started.wait(timeout=1)

    executor.stop()
    external_may_return.set()
    executor.join()

    assert executor.is_alive is False
    assert transitions == []
    assert calls["failures"] == []


def test_exhausted_sweep_runs_even_when_no_job_can_be_claimed() -> None:
    sessions = SessionFactory()
    saga = RecordingSaga(session_factory=sessions)
    exhausted_id = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    executor, calls = build_executor(
        session_factory=sessions,
        saga=saga,
        claim_results=[None],
        sweep_result=(exhausted_id,),
    )

    assert executor.run_once() is True
    assert calls["sweep"] == 1
    assert calls["claim"] == 1
    assert saga.calls == 0


def test_final_transaction_commit_failure_rolls_back_then_records_step_failure() -> None:
    sessions = SessionFactory(fail_commit_number=4)

    class FinalizingSaga:
        executor: DocumentDeletionExecutor

        def run_step(
            self,
            claimed: ClaimedDocumentDeletion,
            *,
            checkpoint: object,
        ) -> None:
            checkpoint()
            deletion_manifest = DocumentDeletionManifest(
                schema_version=1,
                document_id=DOCUMENT_ID,
                bucket_name="rag-documents",
                raw_object_key=f"raw/2026/08/{DOCUMENT_ID}.pdf",
                derived_object_keys=(),
                derived_prefixes=(),
                parse_run_ids=(),
                block_ids=(),
                asset_ids=(),
                chunk_ids=(),
                knowledge_source_relation_ids=(),
                knowledge_item_ids=(),
                search_index_name="casting_chunks_v1",
                search_index_alias="casting_chunks_current",
            )
            self.executor._finalize(claimed, deletion_manifest)

    saga = FinalizingSaga()
    executor, calls = build_executor(
        session_factory=sessions,
        saga=saga,
        claim_results=[claimed_job("finalize_postgresql")],
    )
    saga.executor = executor

    assert executor.run_once() is True

    assert sessions.rollbacks == 1
    assert calls["finalizations"] == [(JOB_ID, DOCUMENT_ID)]
    assert calls["failures"] == [(5, "DOCUMENT_DELETION_STEP_FAILED")]
    assert sessions.active == 0
    assert all(session.closed for session in sessions.created)
