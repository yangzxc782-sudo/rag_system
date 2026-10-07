from contextlib import contextmanager
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import DocumentProcessingJob
from app.services import document_processing as service
from app.tasks.document_processing_executor import DocumentProcessingExecutor
from test_document_processing import db, settings, setup
from app.services.document_graph_builds import _now


def test_executor_default_off_does_not_open_session(settings):
    opened = []
    executor = DocumentProcessingExecutor(settings=settings, session_factory=lambda: opened.append(True))
    executor.start()
    assert not executor.run_once() and not opened


def test_restart_rediscovers_queue_and_does_not_retry_failed_job(db, settings, monkeypatch):
    doc, state, *_ = setup(db, settings, monkeypatch)
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    calls = []
    def advance(session, document_id, job_id, **kwargs):
        assert not session.in_transaction()
        calls.append((document_id, job_id, kwargs["worker_id"]))
        _, job = service._locked(session, document_id, job_id)
        job.last_error_code = "SYNTHETIC_FAILURE"
        service._release(job, "failed", _now(session))
        session.commit()
        return True
    first = DocumentProcessingExecutor(settings=settings, session_factory=factory, advance=advance)
    assert first.run_once()
    first.stop()
    restarted = DocumentProcessingExecutor(settings=settings, session_factory=factory, advance=advance)
    assert not restarted.run_once()
    service.retry_processing(db, doc, state["job_id"], settings=settings)
    assert restarted.run_once()
    assert len(calls) == 2 and calls[0][2] != calls[1][2]


def test_renewal_never_revives_expired_lease_or_changes_fence(db, settings, monkeypatch):
    doc, state, *_ = setup(db, settings, monkeypatch)
    def crash(session, document_id, **kwargs): raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        service.advance_processing(db, doc, state["job_id"], settings=settings, worker_id="owner", parser=crash)
    job = db.get(DocumentProcessingJob, state["job_id"])
    token, fence, count = job.lease_token, job.fencing_token, job.attempt_count
    db.commit()
    assert not service.renew_processing_lease(db, doc, state["job_id"], "other", lease_seconds=600)
    assert service.renew_processing_lease(db, doc, state["job_id"], "owner", lease_seconds=600)
    job = db.get(DocumentProcessingJob, state["job_id"])
    assert (job.lease_token, job.fencing_token, job.attempt_count) == (token, fence, count)
    job.locked_at = _now(db) - timedelta(seconds=601)
    job.lease_expires_at = _now(db) - timedelta(seconds=1)
    db.commit()
    assert not service.renew_processing_lease(db, doc, state["job_id"], "owner", lease_seconds=600)


def test_transient_discovery_failure_does_not_kill_loop(settings):
    settings.document_processing_executor_enabled = True
    settings.document_processing_poll_interval_seconds = 0.1
    seen = Event()
    attempts = []
    @contextmanager
    def unavailable():
        attempts.append(True)
        if len(attempts) >= 2:
            seen.set()
        raise RuntimeError("synthetic database unavailable")
        yield
    executor = DocumentProcessingExecutor(settings=settings, session_factory=unavailable)
    executor.start()
    try:
        assert seen.wait(2)
    finally:
        executor.stop()
        executor.join(2)
    assert not executor._thread.is_alive()
