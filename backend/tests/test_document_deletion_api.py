from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import documents as documents_api
from app.core.errors import (
    DOCUMENT_DELETION_EXECUTOR_DISABLED,
    DOCUMENT_DELETION_STATE_INCONSISTENT,
    BusinessError,
)
from app.main import app
from app.models.document import Document
from app.models.document_deletion_job import DocumentDeletionJob
from app.services import document_deletion as deletion_service


DOCUMENT_ID = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
client = TestClient(app)


class FakeDeletionSession:
    def __init__(
        self,
        document: object | None,
        job: object | None = None,
        *,
        events: list[str] | None = None,
        commit_error: Exception | None = None,
    ) -> None:
        self.document = document
        self.job = job
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.events = events if events is not None else []
        self.commit_error = commit_error

    def scalar(self, statement: object) -> object | None:
        statement_text = str(statement)
        if "FROM documents" in statement_text:
            return self.document
        if "FROM document_deletion_jobs" in statement_text:
            return self.job
        return None

    def get(self, model: object, item_id: UUID) -> object | None:
        if model is Document:
            return self.document
        return None

    def add(self, value: object) -> None:
        self.added.append(value)
        if isinstance(value, DocumentDeletionJob):
            self.job = value

    def flush(self) -> None:
        if isinstance(self.job, DocumentDeletionJob):
            if self.job.id is None:
                self.job.id = uuid4()
            if self.job.created_at is None:
                self.job.created_at = NOW
            if self.job.updated_at is None:
                self.job.updated_at = NOW

    def commit(self) -> None:
        self.commits += 1
        self.events.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollbacks += 1

    def refresh(self, _value: object) -> None:
        return None


class FakeExecutor:
    def __init__(
        self,
        events: list[str],
        *,
        wake_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.wake_error = wake_error
        self.wake_calls = 0

    def wake(self) -> None:
        self.wake_calls += 1
        self.events.append("wake")
        if self.wake_error is not None:
            raise self.wake_error


def _document(deletion_status: str = "normal") -> SimpleNamespace:
    return SimpleNamespace(
        id=DOCUMENT_ID,
        deletion_status=deletion_status,
        process_status="uploaded",
        updated_at=NOW,
    )


def _job(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        document_id=DOCUMENT_ID,
        status=status,
        current_step="delete_minio_raw",
        step_attempts=5,
        max_attempts=5,
        manifest={"schema_version": 1},
        locked_by=None,
        lease_token=None,
        locked_at=None,
        lease_expires_at=None,
        next_retry_at=None,
        last_error_code="SAFE_ERROR",
        updated_at=NOW,
    )


def _enabled_settings(**overrides: object) -> SimpleNamespace:
    values = {
        "document_deletion_executor_enabled": True,
        "document_deletion_max_step_attempts": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _status(status: str = "deleting") -> SimpleNamespace:
    return SimpleNamespace(
        document_id=DOCUMENT_ID,
        status=status,
        step_attempts=1,
        next_retry_at=None,
        last_error_code=None,
        updated_at=NOW,
    )


def _request(
    settings: SimpleNamespace,
    executor: FakeExecutor | None = None,
) -> SimpleNamespace:
    state = SimpleNamespace(settings=settings)
    if executor is not None:
        state.document_deletion_executor = executor
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _response_payload(response: object) -> dict[str, object]:
    return json.loads(response.body)


def _invoke_endpoint(
    operation: str,
    request: SimpleNamespace,
    db: FakeDeletionSession,
) -> object:
    if operation == "delete":
        return documents_api.delete_document_endpoint(request, db, DOCUMENT_ID)
    return documents_api.retry_document_deletion_endpoint(request, db, DOCUMENT_ID)


def test_delete_and_retry_are_disabled_without_state_mutation(monkeypatch) -> None:
    calls: list[str] = []

    def disabled(*args, **kwargs):
        calls.append("checked")
        raise BusinessError(
            DOCUMENT_DELETION_EXECUTOR_DISABLED,
            "Document deletion executor is disabled.",
            status_code=503,
        )

    monkeypatch.setattr(
        documents_api,
        "request_document_deletion",
        disabled,
    )
    monkeypatch.setattr(
        documents_api,
        "retry_document_deletion",
        disabled,
    )

    delete_response = client.delete(f"/api/v1/documents/{DOCUMENT_ID}")
    retry_response = client.post(
        f"/api/v1/documents/{DOCUMENT_ID}/deletion/retry"
    )

    assert delete_response.status_code == 503
    assert retry_response.status_code == 503
    assert delete_response.json()["error"]["code"] == DOCUMENT_DELETION_EXECUTOR_DISABLED
    assert retry_response.json()["error"]["code"] == DOCUMENT_DELETION_EXECUTOR_DISABLED
    assert calls == ["checked", "checked"]


def test_status_remains_readable_while_executor_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        documents_api,
        "get_document_deletion_status",
        lambda db, document_id: _status("retrying"),
    )

    response = client.get(
        f"/api/v1/documents/{DOCUMENT_ID}/deletion-status"
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "retrying"


def test_deletion_response_exposes_only_safe_fields(monkeypatch) -> None:
    monkeypatch.setattr(app.state.settings, "document_deletion_executor_enabled", True)
    monkeypatch.setattr(
        documents_api,
        "request_document_deletion",
        lambda *args, **kwargs: _status(),
    )
    monkeypatch.setattr(documents_api, "_wake_document_deletion_executor", lambda request: None)

    response = client.delete(f"/api/v1/documents/{DOCUMENT_ID}")

    assert response.status_code == 202
    assert set(response.json()["data"]) == {
        "document_id",
        "status",
        "step_attempts",
        "next_retry_at",
        "last_error_code",
        "updated_at",
    }


def test_delete_commits_before_waking_executor(monkeypatch) -> None:
    events: list[str] = []
    db = FakeDeletionSession(_document(), events=events)
    executor = FakeExecutor(events)
    monkeypatch.setattr(
        deletion_service,
        "build_document_deletion_manifest",
        lambda *args, **kwargs: SimpleNamespace(
            to_payload=lambda: {"schema_version": 1}
        ),
    )

    response = documents_api.delete_document_endpoint(
        _request(_enabled_settings(), executor),
        db,
        DOCUMENT_ID,
    )

    assert response.data.status == "deleting"
    assert events == ["commit", "wake"]
    assert db.job.status == "pending"
    assert db.document.deletion_status == "deleting"


def test_delete_commit_failure_rolls_back_and_does_not_wake(monkeypatch) -> None:
    events: list[str] = []
    db = FakeDeletionSession(
        _document(),
        events=events,
        commit_error=RuntimeError("controlled commit failure"),
    )
    executor = FakeExecutor(events)
    monkeypatch.setattr(
        deletion_service,
        "build_document_deletion_manifest",
        lambda *args, **kwargs: SimpleNamespace(
            to_payload=lambda: {"schema_version": 1}
        ),
    )

    with pytest.raises(RuntimeError, match="controlled commit failure"):
        documents_api.delete_document_endpoint(
            _request(_enabled_settings(), executor),
            db,
            DOCUMENT_ID,
        )

    assert events == ["commit"]
    assert executor.wake_calls == 0
    assert db.rollbacks == 1


def test_delete_wake_failure_keeps_committed_job_and_returns_202(
    monkeypatch,
    caplog,
) -> None:
    events: list[str] = []
    db = FakeDeletionSession(_document(), events=events)
    executor = FakeExecutor(
        events,
        wake_error=RuntimeError("sensitive wake detail"),
    )
    monkeypatch.setattr(
        deletion_service,
        "build_document_deletion_manifest",
        lambda *args, **kwargs: SimpleNamespace(
            to_payload=lambda: {"schema_version": 1}
        ),
    )

    response = documents_api.delete_document_endpoint(
        _request(_enabled_settings(), executor),
        db,
        DOCUMENT_ID,
    )

    assert response.data.status == "deleting"
    assert events == ["commit", "wake"]
    assert db.commits == 1
    assert db.rollbacks == 0
    assert db.job.status == "pending"
    warning_text = " ".join(record.getMessage() for record in caplog.records)
    assert "wake failed" in warning_text.lower()
    assert "sensitive wake detail" not in warning_text


def test_retry_commits_before_waking_executor(monkeypatch) -> None:
    events: list[str] = []
    job = _job("delete_failed")
    db = FakeDeletionSession(_document("delete_failed"), job, events=events)
    executor = FakeExecutor(events)

    def fake_retry(_db, _job_id):
        job.status = "pending"
        job.step_attempts = 0
        db.document.deletion_status = "deleting"
        return True

    monkeypatch.setattr(deletion_service, "manual_retry_job", fake_retry)

    response = documents_api.retry_document_deletion_endpoint(
        _request(_enabled_settings(), executor),
        db,
        DOCUMENT_ID,
    )

    assert response.data.status == "deleting"
    assert events == ["commit", "wake"]


def test_retry_commit_failure_rolls_back_and_does_not_wake(monkeypatch) -> None:
    events: list[str] = []
    job = _job("delete_failed")
    db = FakeDeletionSession(
        _document("delete_failed"),
        job,
        events=events,
        commit_error=RuntimeError("controlled retry commit failure"),
    )
    executor = FakeExecutor(events)

    def fake_retry(_db, _job_id):
        job.status = "pending"
        job.step_attempts = 0
        db.document.deletion_status = "deleting"
        return True

    monkeypatch.setattr(deletion_service, "manual_retry_job", fake_retry)

    with pytest.raises(RuntimeError, match="controlled retry commit failure"):
        documents_api.retry_document_deletion_endpoint(
            _request(_enabled_settings(), executor),
            db,
            DOCUMENT_ID,
        )

    assert events == ["commit"]
    assert executor.wake_calls == 0
    assert db.rollbacks == 1


def test_retry_wake_failure_keeps_committed_job_and_returns_202(
    monkeypatch,
) -> None:
    events: list[str] = []
    job = _job("delete_failed")
    db = FakeDeletionSession(_document("delete_failed"), job, events=events)
    executor = FakeExecutor(events, wake_error=RuntimeError("wake unavailable"))

    def fake_retry(_db, _job_id):
        job.status = "pending"
        job.step_attempts = 0
        db.document.deletion_status = "deleting"
        return True

    monkeypatch.setattr(deletion_service, "manual_retry_job", fake_retry)

    response = documents_api.retry_document_deletion_endpoint(
        _request(_enabled_settings(), executor),
        db,
        DOCUMENT_ID,
    )

    assert response.data.status == "deleting"
    assert events == ["commit", "wake"]
    assert db.commits == 1
    assert db.rollbacks == 0
    assert job.status == "pending"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", f"/api/v1/documents/{DOCUMENT_ID}"),
        ("POST", f"/api/v1/documents/{DOCUMENT_ID}/deletion/retry"),
    ],
)
def test_wake_failure_is_nonfatal_at_http_boundary(
    monkeypatch,
    method: str,
    path: str,
) -> None:
    executor = FakeExecutor([], wake_error=RuntimeError("wake unavailable"))
    monkeypatch.setattr(
        app.state,
        "document_deletion_executor",
        executor,
        raising=False,
    )
    monkeypatch.setattr(
        documents_api,
        "request_document_deletion",
        lambda *args, **kwargs: _status(),
    )
    monkeypatch.setattr(
        documents_api,
        "retry_document_deletion",
        lambda *args, **kwargs: _status(),
    )

    response = client.request(method, path)

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "deleting"
    assert executor.wake_calls == 1


@pytest.mark.parametrize("operation", ["delete", "retry"])
@pytest.mark.parametrize(
    ("document_status", "job_status"),
    [
        ("normal", None),
        ("deleting", "pending"),
        ("deleting", "processing"),
        ("deleting", "retry_wait"),
        ("delete_failed", "delete_failed"),
        (None, "pending"),
        (None, "delete_failed"),
        (None, None),
    ],
)
def test_executor_disabled_precedes_all_legal_delete_and_retry_outcomes(
    operation: str,
    document_status: str | None,
    job_status: str | None,
) -> None:
    document = None if document_status is None else _document(document_status)
    job = None if job_status is None else _job(job_status)
    document_before = None if document is None else vars(document).copy()
    job_before = None if job is None else vars(job).copy()
    events: list[str] = []
    db = FakeDeletionSession(document, job, events=events)
    executor = FakeExecutor(events)

    response = _invoke_endpoint(
        operation,
        _request(
            _enabled_settings(document_deletion_executor_enabled=False),
            executor,
        ),
        db,
    )

    assert response.status_code == 503
    assert _response_payload(response)["error"]["code"] == (
        DOCUMENT_DELETION_EXECUTOR_DISABLED
    )
    if document is not None:
        assert vars(document) == document_before
    if job is not None:
        assert vars(job) == job_before
    assert db.added == []
    assert db.commits == 0
    assert db.rollbacks == 1
    assert executor.wake_calls == 0


@pytest.mark.parametrize("operation", ["delete", "retry"])
def test_invalid_invariant_precedes_executor_disabled(
    operation: str,
) -> None:
    events: list[str] = []
    db = FakeDeletionSession(_document("normal"), _job("pending"), events=events)
    executor = FakeExecutor(events)

    response = _invoke_endpoint(
        operation,
        _request(
            _enabled_settings(document_deletion_executor_enabled=False),
            executor,
        ),
        db,
    )

    assert response.status_code == 409
    assert _response_payload(response)["error"]["code"] == (
        DOCUMENT_DELETION_STATE_INCONSISTENT
    )
    assert executor.wake_calls == 0


def test_service_schedules_one_job_and_snapshots_max_attempts(monkeypatch) -> None:
    db = FakeDeletionSession(_document())
    manifest = SimpleNamespace(to_payload=lambda: {"schema_version": 1})
    monkeypatch.setattr(
        deletion_service,
        "build_document_deletion_manifest",
        lambda *args, **kwargs: manifest,
    )

    result = deletion_service.request_document_deletion(
        db,
        DOCUMENT_ID,
        settings=_enabled_settings(document_deletion_max_step_attempts=7),
    )

    assert result.status == "deleting"
    assert db.document.deletion_status == "deleting"
    assert len(db.added) == 1
    assert db.job.max_attempts == 7
    assert db.job.manifest == {"schema_version": 1}
    assert db.commits == 1


def test_duplicate_delete_reuses_active_job_without_creating_another() -> None:
    job = _job("retry_wait")
    job.step_attempts = 2
    db = FakeDeletionSession(_document("deleting"), job)

    result = deletion_service.request_document_deletion(
        db,
        DOCUMENT_ID,
        settings=_enabled_settings(),
    )

    assert result.status == "retrying"
    assert result.step_attempts == 2
    assert db.added == []
    assert db.commits == 1


def test_completed_delete_and_status_return_no_public_row() -> None:
    db = FakeDeletionSession(None, None)

    assert deletion_service.request_document_deletion(
        db,
        DOCUMENT_ID,
        settings=_enabled_settings(),
    ) is None
    assert deletion_service.get_document_deletion_status(db, DOCUMENT_ID) is None


def test_status_inconsistent_pair_is_internal_error() -> None:
    db = FakeDeletionSession(_document("normal"), _job("pending"))

    try:
        deletion_service.get_document_deletion_status(db, DOCUMENT_ID)
    except BusinessError as error:
        assert error.code == DOCUMENT_DELETION_STATE_INCONSISTENT
        assert error.status_code == 500
    else:
        raise AssertionError("status must fail closed on inconsistent pair")


def test_disabled_service_validates_invariant_before_returning_503() -> None:
    db = FakeDeletionSession(_document("normal"), _job("pending"))

    try:
        deletion_service.request_document_deletion(
            db,
            DOCUMENT_ID,
            settings=_enabled_settings(document_deletion_executor_enabled=False),
        )
    except BusinessError as error:
        assert error.code == DOCUMENT_DELETION_STATE_INCONSISTENT
    else:
        raise AssertionError("inconsistent state must fail before disabled gate")

    assert db.document.deletion_status == "normal"
    assert db.rollbacks == 1


def test_absent_document_failed_job_manual_retry_preserves_recovery_identity(
    monkeypatch,
) -> None:
    job = _job("delete_failed")
    original_manifest = job.manifest
    original_step = job.current_step
    original_max = job.max_attempts
    db = FakeDeletionSession(None, job)

    def fake_retry(_db, job_id):
        assert job_id == job.id
        job.status = "pending"
        job.step_attempts = 0
        job.last_error_code = None
        return True

    monkeypatch.setattr(deletion_service, "manual_retry_job", fake_retry)

    result = deletion_service.retry_document_deletion(
        db,
        DOCUMENT_ID,
        settings=_enabled_settings(),
    )

    assert result.status == "deleting"
    assert job.current_step == original_step
    assert job.manifest is original_manifest
    assert job.max_attempts == original_max
    assert job.step_attempts == 0
    assert db.document is None
