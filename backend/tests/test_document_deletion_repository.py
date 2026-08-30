from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.core.errors import DOCUMENT_DELETION_STATE_INCONSISTENT, BusinessError
from app.models.document_deletion_job import DocumentDeletionJob
from app.services.document_deletion_repository import (
    build_advance_step_statement,
    build_claim_statement,
    build_exhausted_sweep_statement,
    build_manual_retry_statement,
    build_record_step_failure_statement,
    build_renew_lease_statement,
    manual_retry_job,
    renew_lease,
    record_step_failure,
    sweep_exhausted_jobs,
)
from app.services.document_deletion_state import validate_document_job_invariant


def _sql(statement: object) -> str:
    return " ".join(
        str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        .lower()
        .split()
    )


@pytest.mark.parametrize(
    ("document_status", "job_status"),
    [
        ("normal", None),
        ("deleting", "pending"),
        ("deleting", "processing"),
        ("deleting", "retry_wait"),
        ("delete_failed", "delete_failed"),
        (None, "pending"),
        (None, "processing"),
        (None, "retry_wait"),
        (None, "delete_failed"),
        (None, None),
    ],
)
def test_document_job_invariant_accepts_only_approved_pairs(
    document_status: str | None,
    job_status: str | None,
) -> None:
    document = None if document_status is None else SimpleNamespace(deletion_status=document_status)
    job = None if job_status is None else SimpleNamespace(status=job_status)

    validate_document_job_invariant(document, job)


@pytest.mark.parametrize(
    ("document_status", "job_status"),
    [
        ("normal", "pending"),
        ("normal", "processing"),
        ("normal", "delete_failed"),
        ("deleting", None),
        ("deleting", "delete_failed"),
        ("delete_failed", None),
        ("delete_failed", "pending"),
        ("delete_failed", "processing"),
        ("delete_failed", "retry_wait"),
    ],
)
def test_document_job_invariant_rejects_every_other_pair_without_mutation(
    document_status: str,
    job_status: str | None,
) -> None:
    document = SimpleNamespace(deletion_status=document_status)
    job = None if job_status is None else SimpleNamespace(status=job_status)

    with pytest.raises(BusinessError) as exc_info:
        validate_document_job_invariant(document, job)

    assert exc_info.value.code == DOCUMENT_DELETION_STATE_INCONSISTENT
    assert document.deletion_status == document_status
    if job is not None:
        assert job.status == job_status


def test_claim_sql_is_atomic_db_time_fenced_and_excludes_exhausted_jobs() -> None:
    statement = build_claim_statement(
        locked_by="worker-1",
        lease_token=uuid4(),
        lease_seconds=120,
    )
    sql = _sql(statement)

    assert "for update skip locked" in sql
    assert "now()" in sql
    assert "step_attempts < document_deletion_jobs.max_attempts" in sql
    assert "status = 'pending'" in sql
    assert "status = 'retry_wait'" in sql
    assert "next_retry_at <= now()" in sql
    assert "status = 'processing'" in sql
    assert "lease_expires_at <= now()" in sql
    assert "step_attempts + 1" in sql
    assert "returning" in sql


def test_exhausted_sweep_sql_is_mutually_exclusive_with_claim() -> None:
    claim_sql = _sql(build_claim_statement(locked_by="worker", lease_token=uuid4(), lease_seconds=120))
    sweep_sql = _sql(build_exhausted_sweep_statement())

    assert "step_attempts < document_deletion_jobs.max_attempts" in claim_sql
    assert "step_attempts >= document_deletion_jobs.max_attempts" in sweep_sql
    assert "status = 'processing'" in sweep_sql
    assert "lease_expires_at <= now()" in sweep_sql
    assert "status='delete_failed'" in sweep_sql.replace(" ", "")
    assert "next_retry_at=null" in sweep_sql.replace(" ", "")


def test_renew_lease_requires_current_unexpired_fence_and_uses_db_time() -> None:
    job_id = uuid4()
    token = uuid4()
    sql = _sql(build_renew_lease_statement(job_id, token, lease_seconds=120))

    assert f"id = '{job_id}'" in sql
    assert "status = 'processing'" in sql
    assert f"lease_token = '{token}'" in sql
    assert "lease_expires_at > now()" in sql
    assert "lease_expires_at=(now()+make_interval" in sql.replace(" ", "")


@pytest.mark.parametrize("rowcount", [0, 1])
def test_renew_lease_returns_false_for_stale_or_expired_owner(rowcount: int) -> None:
    class FakeDb:
        def execute(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(rowcount=rowcount)

    assert renew_lease(FakeDb(), uuid4(), uuid4(), lease_seconds=120) is (rowcount == 1)


def test_new_step_and_manual_retry_reset_step_local_attempts() -> None:
    advance_sql = _sql(build_advance_step_statement(uuid4(), uuid4(), "delete_minio_derived"))
    retry_sql = _sql(build_manual_retry_statement(uuid4()))

    assert "step_attempts=0" in advance_sql.replace(" ", "")
    assert "current_step='delete_minio_derived'" in advance_sql.replace(" ", "")
    assert "status='pending'" in advance_sql.replace(" ", "")
    assert "step_attempts=0" in retry_sql.replace(" ", "")
    assert "status='pending'" in retry_sql.replace(" ", "")
    assert "status = 'delete_failed'" in retry_sql
    assert "max_attempts" not in retry_sql.split(" where ", maxsplit=1)[0]


def test_max_attempts_is_a_per_job_snapshot_used_by_every_attempt_boundary() -> None:
    existing_job = DocumentDeletionJob(document_id=uuid4(), max_attempts=3)
    runtime_default_for_new_jobs = 9
    new_job = DocumentDeletionJob(document_id=uuid4(), max_attempts=runtime_default_for_new_jobs)

    assert existing_job.max_attempts == 3
    assert new_job.max_attempts == 9
    assert existing_job.max_attempts == 3

    claim_sql = _sql(build_claim_statement(locked_by="worker", lease_token=uuid4(), lease_seconds=120))
    failure_sql = _sql(
        build_record_step_failure_statement(
            uuid4(),
            uuid4(),
            retry_seconds=30,
            error_code="STORAGE_UNAVAILABLE",
        )
    )
    sweep_sql = _sql(build_exhausted_sweep_statement())

    assert "step_attempts < document_deletion_jobs.max_attempts" in claim_sql
    assert "step_attempts >= document_deletion_jobs.max_attempts" in failure_sql
    assert "step_attempts >= document_deletion_jobs.max_attempts" in sweep_sql


def test_record_step_failure_uses_db_time_and_fence_for_retry_or_terminal_failure() -> None:
    job_id = uuid4()
    token = uuid4()
    sql = _sql(
        build_record_step_failure_statement(
            job_id,
            token,
            retry_seconds=30,
            error_code="MINIO_UNAVAILABLE",
        )
    )

    assert "status = 'processing'" in sql
    assert f"lease_token = '{token}'" in sql
    assert "lease_expires_at > now()" in sql
    assert "case when (document_deletion_jobs.step_attempts >= document_deletion_jobs.max_attempts)" in sql
    assert "then 'delete_failed' else 'retry_wait'" in sql
    assert "now() + make_interval" in sql
    assert "last_error_code='minio_unavailable'" in sql.replace(" ", "")


def test_record_step_failure_rejects_stale_or_expired_owner() -> None:
    class FakeDb:
        def execute(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(rowcount=0)

    assert (
        record_step_failure(
            FakeDb(),
            uuid4(),
            uuid4(),
            retry_seconds=30,
            error_code="MINIO_UNAVAILABLE",
        )
        is False
    )


def test_terminal_failure_synchronizes_document_state_in_same_session() -> None:
    statements: list[object] = []

    class FakeDb:
        def execute(self, statement: object) -> SimpleNamespace:
            statements.append(statement)
            return SimpleNamespace(rowcount=1)

    assert record_step_failure(
        FakeDb(),
        uuid4(),
        uuid4(),
        retry_seconds=30,
        error_code="MINIO_UNAVAILABLE",
    )
    assert len(statements) == 2
    document_sql = _sql(statements[1])
    assert "update documents set deletion_status='delete_failed'" in document_sql
    assert "document_deletion_jobs.status = 'delete_failed'" in document_sql
    assert "updated_at=now()" in document_sql.replace(" ", "")


def test_manual_retry_synchronizes_existing_failed_document_without_creating_job() -> None:
    statements: list[object] = []

    class FakeDb:
        def execute(self, statement: object) -> SimpleNamespace:
            statements.append(statement)
            return SimpleNamespace(rowcount=1)

    assert manual_retry_job(FakeDb(), uuid4())
    assert len(statements) == 2
    document_sql = _sql(statements[1])
    assert "update documents set deletion_status='deleting'" in document_sql
    assert "document_deletion_jobs.status = 'pending'" in document_sql


def test_absent_document_manual_retry_succeeds_without_creating_or_restoring_document() -> None:
    statements: list[object] = []

    class FakeDb:
        def execute(self, statement: object) -> SimpleNamespace:
            statements.append(statement)
            return SimpleNamespace(rowcount=1 if len(statements) == 1 else 0)

    assert manual_retry_job(FakeDb(), uuid4()) is True
    assert len(statements) == 2
    job_sql = _sql(statements[0])
    document_sql = _sql(statements[1])
    assert "update document_deletion_jobs set" in job_sql
    assert "status='pending'" in job_sql.replace(" ", "")
    assert "step_attempts=0" in job_sql.replace(" ", "")
    assert "max_attempts" not in job_sql.split(" where ", maxsplit=1)[0]
    assert "update documents set deletion_status='deleting'" in document_sql
    assert "insert into documents" not in document_sql
    assert "deletion_status='normal'" not in document_sql.replace(" ", "")


def test_absent_document_exhausted_sweep_marks_job_failed_when_document_update_matches_zero_rows() -> None:
    job_id = uuid4()
    document_statements: list[object] = []

    class ScalarResult:
        def all(self) -> list[object]:
            return [job_id]

    class FakeDb:
        def scalars(self, statement: object) -> ScalarResult:
            assert "status='delete_failed'" in _sql(statement).replace(" ", "")
            return ScalarResult()

        def execute(self, statement: object) -> SimpleNamespace:
            document_statements.append(statement)
            return SimpleNamespace(rowcount=0)

    assert sweep_exhausted_jobs(FakeDb()) == [job_id]
    assert len(document_statements) == 1
    document_sql = _sql(document_statements[0])
    assert "update documents set deletion_status='delete_failed'" in document_sql
    assert "insert into documents" not in document_sql


def test_absent_document_step_failure_keeps_retry_and_terminal_paths_when_document_update_matches_zero_rows() -> None:
    statements: list[object] = []

    class FakeDb:
        def execute(self, statement: object) -> SimpleNamespace:
            statements.append(statement)
            return SimpleNamespace(rowcount=1 if len(statements) == 1 else 0)

    assert record_step_failure(
        FakeDb(),
        uuid4(),
        uuid4(),
        retry_seconds=30,
        error_code="STORAGE_UNAVAILABLE",
    )
    assert len(statements) == 2
    job_sql = _sql(statements[0])
    document_sql = _sql(statements[1])
    assert "then 'delete_failed' else 'retry_wait'" in job_sql
    assert "step_attempts >= document_deletion_jobs.max_attempts" in job_sql
    assert "update documents set deletion_status='delete_failed'" in document_sql
    assert "insert into documents" not in document_sql
