from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from app.core.config import Settings
from app.db.session import SessionLocal
from app.search_engine.client import create_document_deletion_search_engine_client
from app.services.document_deletion import (
    ClaimedDocumentDeletion,
    DeletionSagaProtocol,
    DocumentDeletionSaga,
    advance_claimed_deletion,
    claim_document_deletion,
    fail_claimed_deletion,
    finalize_postgresql_deletion,
    renew_claimed_deletion,
    retry_delay_seconds,
    safe_deletion_error_code,
    sweep_exhausted_deletions,
)
from app.services.document_deletion_manifest import DocumentDeletionManifest
from app.services.document_deletion_storage import DocumentDeletionLeaseLost
from app.services.object_storage import get_document_deletion_minio_client


class DocumentDeletionExecutor:
    """Disabled-by-default in-process executor for durable deletion jobs."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] = SessionLocal,
        settings: Settings,
        worker_id: str | None = None,
        saga: DeletionSagaProtocol | None = None,
        sweep_operation: Callable[[Any], tuple[UUID, ...]] = sweep_exhausted_deletions,
        claim_operation: Callable[..., ClaimedDocumentDeletion | None] = claim_document_deletion,
        renew_operation: Callable[..., bool] = renew_claimed_deletion,
        advance_operation: Callable[..., bool] = advance_claimed_deletion,
        failure_operation: Callable[..., bool] = fail_claimed_deletion,
        finalization_operation: Callable[..., None] = finalize_postgresql_deletion,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._enabled = bool(settings.document_deletion_executor_enabled)
        self._worker_id = worker_id or f"document-deletion-{uuid4()}"
        self._sweep_operation = sweep_operation
        self._claim_operation = claim_operation
        self._renew_operation = renew_operation
        self._advance_operation = advance_operation
        self._failure_operation = failure_operation
        self._finalization_operation = finalization_operation
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._transition_lock = threading.RLock()
        self._thread: threading.Thread | None = None

        if saga is not None:
            self._saga: DeletionSagaProtocol | None = saga
        elif self._enabled:
            self._saga = DocumentDeletionSaga(
                settings=settings,
                minio_client=get_document_deletion_minio_client(settings=settings),
                opensearch_client=create_document_deletion_search_engine_client(settings),
                advance_step=self._advance,
                finalize_step=self._finalize,
            )
        else:
            self._saga = None

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(self) -> None:
        if not self._enabled:
            return
        with self._lifecycle_lock:
            if self.is_alive:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name=self._worker_id,
                daemon=True,
            )
            self._thread.start()
            self._wake_event.set()

    def wake(self) -> None:
        if self._enabled:
            self._wake_event.set()

    def stop(self) -> None:
        with self._transition_lock:
            self._stop_event.set()
            self._wake_event.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        resolved_timeout = (
            float(self._settings.document_deletion_shutdown_grace_seconds)
            if timeout is None
            else timeout
        )
        thread.join(timeout=resolved_timeout)

    def run_once(self) -> bool:
        if not self._enabled or self._stop_event.is_set():
            return False

        swept_ids = self._session_call(self._sweep_operation)
        if self._stop_event.is_set():
            return bool(swept_ids)

        claimed = self._session_call(
            lambda db: self._claim_operation(
                db,
                locked_by=self._worker_id,
                lease_token=uuid4(),
                lease_seconds=int(self._settings.document_deletion_lease_seconds),
            )
        )
        if claimed is None:
            return bool(swept_ids)
        if self._saga is None:
            raise RuntimeError("Enabled document deletion executor has no Saga.")

        try:
            self._saga.run_step(
                claimed,
                checkpoint=lambda: self._checkpoint(claimed),
            )
        except DocumentDeletionLeaseLost:
            return True
        except Exception as exc:
            if self._stop_event.is_set():
                return True
            self._record_failure(claimed, safe_deletion_error_code(exc))
        return True

    def _run_loop(self) -> None:
        poll_seconds = float(
            self._settings.document_deletion_poll_interval_seconds
        )
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=poll_seconds)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            while not self._stop_event.is_set() and self.run_once():
                pass

    def _checkpoint(self, claimed: ClaimedDocumentDeletion) -> None:
        if self._stop_event.is_set():
            raise DocumentDeletionLeaseLost()
        renewed = self._session_call(
            lambda db: self._renew_operation(
                db,
                claimed,
                lease_seconds=int(self._settings.document_deletion_lease_seconds),
            )
        )
        if self._stop_event.is_set() or not renewed:
            raise DocumentDeletionLeaseLost()

    def _advance(
        self,
        claimed: ClaimedDocumentDeletion,
        next_step: str,
    ) -> None:
        with self._transition_lock:
            if self._stop_event.is_set():
                raise DocumentDeletionLeaseLost()
            advanced = self._session_call(
                lambda db: self._advance_operation(
                    db,
                    claimed,
                    next_step=next_step,
                )
            )
            if not advanced:
                raise DocumentDeletionLeaseLost()

    def _finalize(
        self,
        claimed: ClaimedDocumentDeletion,
        manifest: DocumentDeletionManifest,
    ) -> None:
        with self._transition_lock:
            if self._stop_event.is_set():
                raise DocumentDeletionLeaseLost()
            self._session_call(
                lambda db: self._finalization_operation(
                    db,
                    claimed=claimed,
                    manifest=manifest,
                )
            )

    def _record_failure(
        self,
        claimed: ClaimedDocumentDeletion,
        error_code: str,
    ) -> None:
        with self._transition_lock:
            if self._stop_event.is_set():
                return
            retry_seconds = retry_delay_seconds(
                claimed.step_attempts,
                base_seconds=int(
                    self._settings.document_deletion_retry_base_seconds
                ),
                max_seconds=int(
                    self._settings.document_deletion_retry_max_seconds
                ),
            )
            self._session_call(
                lambda db: self._failure_operation(
                    db,
                    claimed,
                    retry_seconds=retry_seconds,
                    error_code=error_code,
                )
            )

    def _session_call(self, operation: Callable[[Any], Any]) -> Any:
        session = self._session_factory()
        try:
            result = operation(session)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
