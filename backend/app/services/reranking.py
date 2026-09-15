"""Process-local reranker admission, bounded caller wait, and validated score ordering.

M1 deliberately has no RAG/Hybrid imports or startup hook. M2 owns that wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from threading import Condition, Event, Lock, Thread, current_thread
from time import monotonic
from typing import Callable

from app.core.config import Settings, get_settings
from app.retrieval.local_cross_encoder import LocalCrossEncoderProvider
from app.retrieval.reranker import (
    FAILURE_REASONS, MODEL_ID, FailureReason, RerankBudget, RerankError, RerankerConfig,
    RerankerProvider, RerankRequest, RerankResult, validate_request, validate_result,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True, repr=False)
class _Task:
    request: RerankRequest
    budget: RerankBudget
    done: Event = field(default_factory=Event)
    result: RerankResult | None = None


class RerankingService:
    def __init__(
        self, settings: Settings, *,
        provider_factory: Callable[[RerankerConfig], RerankerProvider] = LocalCrossEncoderProvider,
    ) -> None:
        self._enabled = settings.reranker_enabled
        self._config: RerankerConfig | None = None
        if self._enabled:
            try:
                self._config = RerankerConfig.from_settings(settings)
            except RerankError:
                pass  # Incomplete active profile is unavailable, without ML imports/load.
        self._factory = provider_factory
        self._provider: RerankerProvider | None = None
        self._load_failed = False
        self._condition = Condition(Lock())
        self._task: _Task | None = None  # Exactly one slot, including a timed-out running task.
        self._thread: Thread | None = None
        self._closing = False
        self._closed = Event()

    def rerank(self, request: RerankRequest) -> RerankResult:
        started = monotonic()
        result = self._submit_and_wait(request, started)
        cfg = self._config
        logger.info(
            "reranker model=%s device=%s dtype=%s candidate_count=%d batch_size=%s "
            "latency_ms=%.3f busy=%s timeout=%s fallback_reason=%s",
            MODEL_ID, cfg.device if cfg else None, cfg.dtype if cfg else None,
            len(request.candidates), cfg.batch_size if cfg else None,
            (monotonic() - started) * 1000, result.failure_reason == "busy",
            result.failure_reason == "timeout", result.failure_reason,
        )
        return result

    def _submit_and_wait(self, request: RerankRequest, started: float) -> RerankResult:
        def failed(reason: FailureReason) -> RerankResult:
            return RerankResult(request.request_id, failure_reason=reason)

        with self._condition:
            if self._closing:
                return failed("closed")
            if not self._enabled:
                return failed("disabled")
            if self._config is None:
                return failed("configuration_invalid")
            if self._task is not None:
                return failed("busy")
            if self._load_failed:
                return failed("unavailable")
            try:
                validate_request(request)
            except (RerankError, TypeError, AttributeError):
                return failed("invalid_request")
            if not request.candidates:
                return RerankResult(request.request_id)
            task = _Task(request, RerankBudget(started + self._config.timeout_seconds))
            self._task = task
            if self._thread is None:
                self._thread = Thread(target=self._run, name="local-reranker", daemon=True)
                try:
                    self._thread.start()
                except Exception:
                    self._thread = None
                    self._task = None
                    self._load_failed = True
                    return failed("unavailable")
            self._condition.notify_all()

        task.done.wait(max(0.0, task.budget.deadline - monotonic()))
        with self._condition:
            # Caller and worker use this lock to arbitrate completion vs timeout.
            if self._closing:
                task.budget.cancelled.set()
                task.result = None
                return failed("closed")
            if monotonic() >= task.budget.deadline or not task.done.is_set():
                task.budget.cancelled.set()
                task.result = None
                return failed("timeout")
            result = task.result
            task.result = None
            return result if result is not None else failed("timeout")

    def _execute(self, task: _Task) -> RerankResult:
        try:
            task.budget.check()
            if self._provider is None:
                try:
                    assert self._config is not None
                    self._provider = self._factory(self._config)
                except Exception:
                    self._load_failed = True
                    return RerankResult(task.request.request_id, failure_reason="unavailable")
            task.budget.check()
            result = self._provider.rerank(task.request, budget=task.budget)
            task.budget.check()
            if result.failure_reason is not None:
                reason = result.failure_reason if result.failure_reason in FAILURE_REASONS else "invalid_output"
                return RerankResult(task.request.request_id, failure_reason=reason)
            validate_result(task.request, result)
            return RerankResult(result.request_id, tuple(sorted(
                result.scores, key=lambda score: (-score.raw_score, score.original_rank, score.chunk_id),
            )))
        except RerankError as exc:
            return RerankResult(task.request.request_id, failure_reason=exc.reason)
        except Exception:
            return RerankResult(task.request.request_id, failure_reason="inference_exception")

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._task is not None or self._closing)
                    if self._task is None:
                        return
                    task = self._task
                result = self._execute(task)
                with self._condition:
                    if (not self._closing and not task.budget.cancelled.is_set()
                            and monotonic() < task.budget.deadline):
                        task.result = result
                    # The task slot is released only after actual provider return and cleanup.
                    self._task = None
                    task.done.set()
                    self._condition.notify_all()
                    if self._closing:
                        return
                # Release the worker's references to content and scores while idle.
                del task, result
        finally:
            try:
                if self._provider is not None:
                    self._provider.close()
            finally:
                self._provider = None
                self._closed.set()

    def wait_idle(self, timeout: float) -> bool:
        """Bounded observation for lifecycle checks; never queues/admit work."""
        with self._condition:
            return self._condition.wait_for(lambda: self._task is None, timeout)

    def wait_closed(self, timeout: float) -> bool:
        return self._closed.wait(timeout)

    def close(self) -> None:
        with self._condition:
            self._closing = True
            if self._task is not None:
                self._task.budget.cancelled.set()
                self._task.result = None
                self._task.done.set()  # Release caller; the worker remains occupied until done.
            thread = self._thread
            self._condition.notify_all()
            if thread is None:
                self._closed.set()
        if thread is not None and thread is not current_thread():
            # Python cannot safely kill an in-flight CUDA forward. Shutdown waits naturally.
            thread.join()


_cache_lock = Lock()
_service_cache: RerankingService | None = None


def get_reranking_service(settings: Settings | None = None) -> RerankingService:
    global _service_cache
    with _cache_lock:
        if _service_cache is None:
            _service_cache = RerankingService(settings if settings is not None else get_settings())
        return _service_cache


def close_reranking_service() -> None:
    with _cache_lock:
        service = _service_cache
    if service is not None:
        service.close()
    # Retain the closed singleton: even a concurrent getter cannot create a second runtime.
    # Config reload/retry after terminal failure requires application restart.
