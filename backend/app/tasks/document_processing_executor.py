"""Default-off local executor; SQL jobs remain authoritative across restarts."""
import logging
import threading
from uuid import uuid4

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Document, DocumentProcessingJob
from app.services.document_processing import advance_processing, renew_processing_lease

logger = logging.getLogger(__name__)


class DocumentProcessingExecutor:
    def __init__(self, *, settings, session_factory=SessionLocal, advance=advance_processing):
        if settings.document_processing_executor_enabled and session_factory is SessionLocal:
            from sqlalchemy.engine import make_url
            if SessionLocal.kw["bind"].url != make_url(settings.database_url):
                raise ValueError("DOCUMENT_PROCESSING_DATABASE_MISMATCH")
        self.settings, self.sessions, self.advance = settings, session_factory, advance
        self.worker_id = f"pdf-processing-{uuid4()}"
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread = None

    def start(self):
        if not self.settings.document_processing_executor_enabled or self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self.worker_id, daemon=True)
        self._thread.start()
        self.wake()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(self.settings.document_processing_shutdown_grace_seconds if timeout is None else timeout)

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(self.settings.document_processing_poll_interval_seconds)
            self._wake.clear()
            while not self._stop.is_set():
                try:
                    if not self.run_once():
                        break
                except Exception as exc:
                    # Includes schema/database outage; keep the executor alive,
                    # back off, and never emit credentials/remote exception text.
                    logger.warning("PDF task executor unavailable: %s", type(exc).__name__)
                    break

    def _heartbeat(self, finished, document_id, job_id):
        while not finished.wait(min(30, self.settings.kg_lease_seconds / 3)):
            try:
                with self.sessions() as db:
                    renew_processing_lease(db, document_id, job_id, self.worker_id,
                        lease_seconds=self.settings.kg_lease_seconds)
            except Exception as exc:
                logger.warning("PDF task lease renewal failed: %s", type(exc).__name__)

    def run_once(self):
        if not self.settings.document_processing_executor_enabled or self._stop.is_set():
            return False
        with self.sessions() as db:
            # Discovery holds no claim lock; stage claim serializes Document first.
            candidate = db.execute(select(DocumentProcessingJob.document_id, DocumentProcessingJob.id)
                .join(Document, Document.id == DocumentProcessingJob.document_id)
                .where(Document.deletion_status == "normal", DocumentProcessingJob.status == "queued",
                    DocumentProcessingJob.checkpoint["pipeline"]["version"].as_integer() == 1)
                .order_by(DocumentProcessingJob.updated_at, DocumentProcessingJob.id).limit(1)).first()
        if candidate is None or self._stop.is_set():
            return False
        doc_id, job_id = candidate
        finished = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(finished, doc_id, job_id), daemon=True)
        heartbeat.start()
        try:
            with self.sessions() as db:
                return self.advance(db, doc_id, job_id, settings=self.settings, worker_id=self.worker_id)
        except Exception as exc:
            logger.warning("PDF task stage stopped: job=%s error=%s", job_id, type(exc).__name__)
            return False
        finally:
            finished.set()
            heartbeat.join(timeout=1)
