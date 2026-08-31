from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.services.document_deletion import (
    ClaimedDocumentDeletion,
    DocumentDeletionSaga,
    build_exhausted_recovery_select_statement,
    claim_document_deletion,
    finalize_postgresql_deletion,
    retry_delay_seconds,
    sweep_exhausted_deletions,
)
from app.services.document_deletion_manifest import DocumentDeletionManifest
from app.services.document_deletion_repository import (
    build_record_step_failure_statement,
)
from app.services.document_deletion_storage import DocumentDeletionLeaseLost


DOCUMENT_ID = UUID("11111111-1111-1111-1111-111111111111")
JOB_ID = UUID("22222222-2222-2222-2222-222222222222")
LEASE_TOKEN = UUID("33333333-3333-3333-3333-333333333333")
PARSE_RUN_ID = UUID("44444444-4444-4444-4444-444444444444")
BLOCK_ID = UUID("55555555-5555-5555-5555-555555555555")
ASSET_ID = UUID("66666666-6666-6666-6666-666666666666")
CHUNK_ID = UUID("77777777-7777-7777-7777-777777777777")


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "document_deletion_executor_enabled": True,
        "llm_provider": "local",
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "test-model",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def manifest() -> DocumentDeletionManifest:
    prefix = f"parsed-assets/{DOCUMENT_ID}/{PARSE_RUN_ID}/"
    return DocumentDeletionManifest(
        schema_version=1,
        document_id=DOCUMENT_ID,
        bucket_name="rag-documents",
        raw_object_key=f"raw/2026/08/{DOCUMENT_ID}.pdf",
        derived_object_keys=(f"{prefix}output.json",),
        derived_prefixes=(prefix,),
        parse_run_ids=(PARSE_RUN_ID,),
        block_ids=(BLOCK_ID,),
        asset_ids=(ASSET_ID,),
        chunk_ids=(CHUNK_ID,),
        knowledge_source_relation_ids=(),
        knowledge_item_ids=(),
        search_index_name="casting_chunks_v1",
        search_index_alias="casting_chunks_current",
    )


def claim(step: str, *, attempts: int = 1) -> ClaimedDocumentDeletion:
    return ClaimedDocumentDeletion(
        job_id=JOB_ID,
        document_id=DOCUMENT_ID,
        current_step=step,
        step_attempts=attempts,
        max_attempts=5,
        manifest=manifest().to_payload(),
        lease_token=LEASE_TOKEN,
    )


def test_settings_keep_executor_disabled_and_validate_lease_timeout_ratio() -> None:
    defaults = Settings(_env_file=None)

    assert defaults.document_deletion_executor_enabled is False
    assert defaults.document_deletion_max_step_attempts == 5
    assert defaults.document_deletion_retry_base_seconds == 5
    assert defaults.document_deletion_retry_max_seconds == 300
    assert defaults.document_deletion_lease_seconds == 120
    assert defaults.document_deletion_poll_interval_seconds == 5
    assert defaults.document_deletion_shutdown_grace_seconds == 10
    assert defaults.document_deletion_storage_timeout_seconds == 30
    assert defaults.document_deletion_heartbeat_object_interval == 25

    assert settings(
        document_deletion_lease_seconds=120,
        document_deletion_storage_timeout_seconds=30,
    ).document_deletion_lease_seconds == 120
    assert settings(
        document_deletion_lease_seconds=90,
        document_deletion_storage_timeout_seconds=30,
    ).document_deletion_lease_seconds == 90

    with pytest.raises(ValueError, match="lease"):
        settings(
            document_deletion_lease_seconds=89,
            document_deletion_storage_timeout_seconds=30,
        )

    assert settings(
        document_deletion_shutdown_grace_seconds=0,
    ).document_deletion_shutdown_grace_seconds == 0
    with pytest.raises(ValueError, match="shutdown_grace"):
        settings(document_deletion_shutdown_grace_seconds=-1)


def test_retry_delay_uses_approved_step_local_schedule_and_cap() -> None:
    assert [
        retry_delay_seconds(attempt, base_seconds=5, max_seconds=300)
        for attempt in range(1, 5)
    ] == [5, 15, 45, 135]
    assert retry_delay_seconds(4, base_seconds=10, max_seconds=100) == 100

    terminal_sql = " ".join(
        str(
            build_record_step_failure_statement(
                JOB_ID,
                LEASE_TOKEN,
                retry_seconds=135,
                error_code="STORAGE_UNAVAILABLE",
            ).compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        .lower()
        .split()
    )
    assert "step_attempts >= document_deletion_jobs.max_attempts" in terminal_sql
    assert "then 'delete_failed' else 'retry_wait'" in terminal_sql
    assert "then null else now() + make_interval" in terminal_sql


def test_exhausted_recovery_sql_is_db_time_exclusive_and_skip_locked() -> None:
    sql = " ".join(
        str(
            build_exhausted_recovery_select_statement().compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        .lower()
        .split()
    )

    assert "status = 'processing'" in sql
    assert "lease_expires_at <= now()" in sql
    assert "step_attempts >= document_deletion_jobs.max_attempts" in sql
    assert "for update skip locked" in sql


def test_exhausted_recovery_accepts_absent_document_and_marks_only_job_failed() -> None:
    job = SimpleNamespace(
        id=JOB_ID,
        document_id=DOCUMENT_ID,
        status="processing",
        step_attempts=5,
        max_attempts=5,
        locked_by="worker",
        lease_token=LEASE_TOKEN,
        locked_at=object(),
        lease_expires_at=object(),
        next_retry_at=None,
        last_error_code=None,
        updated_at=None,
    )

    class ScalarRows:
        def all(self) -> list[object]:
            return [job]

    class FakeDb:
        flushes = 0

        def scalars(self, _statement: object) -> ScalarRows:
            return ScalarRows()

        def scalar(self, _statement: object) -> None:
            return None

        def flush(self) -> None:
            self.flushes += 1

    db = FakeDb()

    assert sweep_exhausted_deletions(db) == (JOB_ID,)
    assert job.status == "delete_failed"
    assert job.lease_token is None
    assert job.last_error_code == "DOCUMENT_DELETION_RETRIES_EXHAUSTED"
    assert db.flushes == 1


def test_claim_accepts_absent_document_and_snapshots_the_fenced_job() -> None:
    job = SimpleNamespace(
        id=JOB_ID,
        document_id=DOCUMENT_ID,
        status="processing",
        current_step="delete_minio_raw",
        step_attempts=2,
        max_attempts=5,
        manifest=manifest().to_payload(),
        lease_token=LEASE_TOKEN,
    )

    class ClaimResult:
        def scalar_one_or_none(self) -> object:
            return job

    class FakeDb:
        def execute(self, _statement: object) -> ClaimResult:
            return ClaimResult()

        def scalar(self, _statement: object) -> None:
            return None

    claimed = claim_document_deletion(
        FakeDb(),
        locked_by="worker",
        lease_token=LEASE_TOKEN,
        lease_seconds=120,
    )

    assert claimed is not None
    assert claimed.document_id == DOCUMENT_ID
    assert claimed.current_step == "delete_minio_raw"
    assert claimed.step_attempts == 2
    assert claimed.lease_token == LEASE_TOKEN


@pytest.mark.parametrize(
    ("step", "expected_next", "expected_call"),
    [
        ("delete_opensearch", "delete_minio_derived", "opensearch"),
        ("delete_minio_derived", "delete_minio_raw", "minio-derived"),
        ("delete_minio_raw", "finalize_postgresql", "minio-raw"),
    ],
)
def test_saga_external_steps_advance_once_and_inject_current_settings(
    step: str,
    expected_next: str,
    expected_call: str,
) -> None:
    calls: list[object] = []

    def checkpoint() -> None:
        calls.append("checkpoint")

    def delete_opensearch(_manifest: object, **kwargs: object) -> None:
        calls.append(
            (
                "opensearch",
                kwargs["current_index_name"],
                kwargs["current_index_alias"],
                kwargs["timeout_seconds"],
            )
        )

    def delete_derived(_manifest: object, **kwargs: object) -> None:
        calls.append(
            (
                "minio-derived",
                kwargs["heartbeat_interval_seconds"],
                kwargs["heartbeat_object_interval"],
            )
        )

    def delete_raw(_manifest: object, **kwargs: object) -> None:
        calls.append(
            (
                "minio-raw",
                kwargs["heartbeat_interval_seconds"],
                kwargs["heartbeat_object_interval"],
            )
        )

    saga = DocumentDeletionSaga(
        settings=settings(search_index_name="current-v2", search_index_alias="current-alias-v2"),
        minio_client=object(),
        opensearch_client=object(),
        advance_step=lambda claimed, next_step: calls.append(("advance", claimed.job_id, next_step)),
        finalize_step=lambda _claimed, _manifest: calls.append("finalize"),
        opensearch_delete=delete_opensearch,
        minio_derived_delete=delete_derived,
        minio_raw_delete=delete_raw,
    )

    saga.run_step(claim(step), checkpoint=checkpoint)

    flattened = [value[0] if isinstance(value, tuple) else value for value in calls]
    assert expected_call in flattened
    assert calls[-1] == ("advance", JOB_ID, expected_next)
    if step == "delete_opensearch":
        assert (
            "opensearch",
            "current-v2",
            "current-alias-v2",
            30,
        ) in calls
    else:
        assert (expected_call, 40.0, 25) in calls


@pytest.mark.parametrize(
    "step",
    ["delete_opensearch", "delete_minio_derived", "delete_minio_raw"],
)
def test_saga_external_failure_never_advances(step: str) -> None:
    advances: list[str] = []

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("controlled-storage-failure")

    saga = DocumentDeletionSaga(
        settings=settings(),
        minio_client=object(),
        opensearch_client=object(),
        advance_step=lambda _claimed, next_step: advances.append(next_step),
        finalize_step=lambda _claimed, _manifest: None,
        opensearch_delete=fail,
        minio_derived_delete=fail,
        minio_raw_delete=fail,
    )

    with pytest.raises(RuntimeError, match="controlled-storage-failure"):
        saga.run_step(claim(step), checkpoint=lambda: None)

    assert advances == []


def test_external_success_returned_after_shutdown_cannot_advance() -> None:
    stopped = False
    advances: list[str] = []

    def checkpoint() -> None:
        if stopped:
            raise DocumentDeletionLeaseLost()

    def external_call(*_args: object, **_kwargs: object) -> None:
        nonlocal stopped
        stopped = True

    saga = DocumentDeletionSaga(
        settings=settings(),
        minio_client=object(),
        opensearch_client=object(),
        advance_step=lambda _claimed, next_step: advances.append(next_step),
        finalize_step=lambda _claimed, _manifest: None,
        opensearch_delete=external_call,
    )

    with pytest.raises(DocumentDeletionLeaseLost):
        saga.run_step(claim("delete_opensearch"), checkpoint=checkpoint)

    assert advances == []


def test_final_step_calls_one_finalizer_and_never_advances() -> None:
    calls: list[str] = []
    saga = DocumentDeletionSaga(
        settings=settings(),
        minio_client=object(),
        opensearch_client=object(),
        advance_step=lambda _claimed, _next: calls.append("advance"),
        finalize_step=lambda _claimed, _manifest: calls.append("finalize"),
    )

    saga.run_step(claim("finalize_postgresql"), checkpoint=lambda: calls.append("checkpoint"))

    assert calls == ["checkpoint", "finalize"]


class RecordingFinalizationSession:
    def __init__(self, *, document_exists: bool) -> None:
        self.document_exists = document_exists
        self.scalar_calls = 0
        self.statements: list[str] = []
        self.flushes = 0

    def scalar(self, statement: object) -> object | None:
        self.scalar_calls += 1
        if self.scalar_calls == 1:
            return SimpleNamespace(
                id=JOB_ID,
                document_id=DOCUMENT_ID,
                status="processing",
                current_step="finalize_postgresql",
                lease_token=LEASE_TOKEN,
            )
        if self.scalar_calls == 2 and self.document_exists:
            return SimpleNamespace(id=DOCUMENT_ID, deletion_status="deleting")
        return None

    def execute(self, statement: object) -> SimpleNamespace:
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.statements.append(sql)
        return SimpleNamespace(rowcount=1)

    def flush(self) -> None:
        self.flushes += 1


@pytest.mark.parametrize("document_exists", [True, False])
def test_finalization_deletes_job_last_and_allows_absent_document_recovery(
    document_exists: bool,
) -> None:
    db = RecordingFinalizationSession(document_exists=document_exists)
    cleanup_calls: list[UUID] = []

    finalize_postgresql_deletion(
        db,
        claimed=claim("finalize_postgresql"),
        manifest=manifest(),
        knowledge_cleanup=lambda document_id: cleanup_calls.append(document_id),
    )

    assert cleanup_calls == [DOCUMENT_ID]
    deletes = [sql for sql in db.statements if sql.startswith("DELETE")]
    assert "document_chunk_blocks" in deletes[0]
    assert "knowledge_item_chunks" in deletes[1]
    assert "document_chunks" in deletes[2]
    assert "document_assets" in deletes[3]
    assert "document_blocks" in deletes[4]
    assert "document_parse_runs" in deletes[5]
    assert "documents" in deletes[6]
    assert "document_deletion_jobs" in deletes[-1]
    assert db.flushes >= 1


def test_finalization_failure_before_job_delete_leaves_job_for_transaction_rollback() -> None:
    db = RecordingFinalizationSession(document_exists=True)

    def fail_cleanup(_document_id: UUID) -> None:
        raise RuntimeError("controlled-finalization-failure")

    with pytest.raises(RuntimeError, match="controlled-finalization-failure"):
        finalize_postgresql_deletion(
            db,
            claimed=claim("finalize_postgresql"),
            manifest=manifest(),
            knowledge_cleanup=fail_cleanup,
        )

    assert not any("document_deletion_jobs" in sql for sql in db.statements)
