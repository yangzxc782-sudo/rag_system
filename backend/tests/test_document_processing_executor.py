from contextlib import contextmanager
import asyncio
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

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


def fake_executor(monkeypatch, advance, *, renew=lambda *a, **kw: None, grace=0.02):
    """Real executor threads; discovery/lease/advance use only in-memory fakes."""
    from app.tasks import document_processing_executor as module
    from uuid import uuid4
    candidate = (uuid4(), uuid4())
    discoveries = []
    @contextmanager
    def sessions():
        def execute(_):
            discoveries.append(True)
            return SimpleNamespace(first=lambda: candidate)
        yield SimpleNamespace(execute=execute)
    monkeypatch.setattr(module, "renew_processing_lease", renew)
    config = SimpleNamespace(document_processing_executor_enabled=True,
        document_processing_poll_interval_seconds=0.01,
        document_processing_shutdown_grace_seconds=grace, kg_lease_seconds=0.03)
    return DocumentProcessingExecutor(settings=config, session_factory=sessions, advance=advance), discoveries


def test_shutdown_drains_stage_keeps_heartbeat_and_stops_new_admission(monkeypatch, caplog):
    entered, release, renewed, completed = Event(), Event(), Event(), Event()
    def advance(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(3)
            return True
        finally:
            completed.set()
    executor, discoveries = fake_executor(monkeypatch, advance, renew=lambda *a, **kw: renewed.set())
    executor.start()
    try:
        assert entered.wait(2)
        async def drain():
            task = asyncio.create_task(executor.shutdown())
            await asyncio.sleep(0.08)
            assert executor._stop.is_set() and not task.done()
            assert not executor.join(0)
            assert renewed.is_set() and executor._heartbeat_thread.is_alive()
            assert len(discoveries) == 1
            assert not executor.run_once()  # stop also guards explicit one-step calls
            assert len(discoveries) == 1
            assert "pdf_executor_shutdown_waiting" in caplog.text
            assert "shutdown complete" not in caplog.text
            release.set()
            await asyncio.wait_for(task, 2)
        asyncio.run(drain())
        assert completed.is_set() and executor.join(0)
        assert not executor._thread.is_alive() and not executor._heartbeat_thread.is_alive()
    finally:
        release.set()
        executor.stop()
        executor.join(2)


def test_shutdown_waits_for_inflight_heartbeat_after_stage_finishes(monkeypatch, caplog):
    renewal_entered, renewal_release, renewal_finally = Event(), Event(), Event()
    stage_finished = Event()
    def renew(*args, **kwargs):
        renewal_entered.set()
        try:
            assert renewal_release.wait(3)
        finally:
            renewal_finally.set()
    def advance(*args, **kwargs):
        assert renewal_entered.wait(2)
        stage_finished.set()
        return True
    executor, discoveries = fake_executor(monkeypatch, advance, renew=renew)
    executor.start()
    try:
        assert stage_finished.wait(2)
        async def drain():
            task = asyncio.create_task(executor.shutdown())
            await asyncio.sleep(0.08)
            assert not task.done() and not executor.join(0)
            assert not renewal_finally.is_set()
            assert '"heartbeat_alive":true' in caplog.text
            assert len(discoveries) == 1
            renewal_release.set()
            await asyncio.wait_for(task, 2)
        asyncio.run(drain())
        assert renewal_finally.is_set() and executor.join(0)
    finally:
        renewal_release.set()
        executor.stop()
        executor.join(2)


def test_stop_during_discovery_prevents_stage_admission(monkeypatch):
    def advance(*a, **kw):
        pytest.fail("stage admitted after stop")
    executor, _ = fake_executor(monkeypatch, advance)
    @contextmanager
    def sessions():
        executor.stop()
        yield SimpleNamespace(execute=lambda _: SimpleNamespace(first=lambda: ("doc", "job")))
    executor.sessions = sessions
    assert not executor.run_once()
    assert executor._heartbeat_thread is None and executor.join(0)


def test_stage_exception_remains_observable_and_heartbeat_is_joined(monkeypatch, caplog):
    def advance(*a, **kw):
        raise ValueError("SENSITIVE_STAGE_BODY")
    executor, _ = fake_executor(monkeypatch, advance)
    assert not executor.run_once()
    assert executor.join(0)
    assert "error=ValueError" in caplog.text and "SENSITIVE_STAGE_BODY" not in caplog.text


def test_blocked_wait_warns_repeatedly_including_zero_grace(monkeypatch, caplog):
    executor, _ = fake_executor(monkeypatch, lambda *a, **kw: None, grace=0)
    waits = []
    def join(timeout):
        waits.append(timeout)
        return len(waits) == 4
    monkeypatch.setattr(executor, "join", join)
    asyncio.run(executor.shutdown())
    assert waits == [0, 1.0, 1.0, 1.0]
    assert caplog.text.count("pdf_executor_shutdown_waiting") == 3


def test_join_checks_remaining_heartbeat_even_without_worker(monkeypatch):
    executor, _ = fake_executor(monkeypatch, lambda *a, **kw: None)
    release = Event()
    from threading import Thread
    executor._heartbeat_thread = Thread(target=release.wait, daemon=True)
    executor._heartbeat_thread.start()
    try:
        assert not executor.join(0)
    finally:
        release.set()
        assert executor.join(2)
