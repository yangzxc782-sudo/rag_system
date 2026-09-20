"""Internal cross-encoder contracts; no ML imports or search/API dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, Callable, Literal, Protocol, get_args
from uuid import uuid4

if TYPE_CHECKING:
    from app.core.config import Settings


MODEL_ID = "BAAI/bge-reranker-v2-m3"
FailureReason = Literal[
    "disabled", "configuration_invalid", "closed", "busy", "timeout",
    "unavailable", "invalid_request", "invalid_output", "query_too_long",
    "query_truncated", "oom", "inference_exception",
]
FAILURE_REASONS = frozenset(get_args(FailureReason))


class RerankError(Exception):
    """Only a fixed reason crosses the model/worker boundary; no upstream text."""

    def __init__(self, reason: FailureReason):
        self.reason = reason if reason in FAILURE_REASONS else "invalid_output"
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    chunk_id: str
    original_rank: int
    content: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RerankRequest:
    request_id: str = field(repr=False)
    query: str = field(repr=False)
    candidates: tuple[RerankCandidate, ...]

    def __post_init__(self) -> None:
        # Snapshot a caller-provided list before a task can outlive the caller.
        object.__setattr__(self, "candidates", tuple(self.candidates))


@dataclass(frozen=True, slots=True)
class RerankScore:
    chunk_id: str
    original_rank: int
    raw_score: float


@dataclass(frozen=True, slots=True)
class RerankResult:
    request_id: str = field(repr=False)
    scores: tuple[RerankScore, ...] = ()
    failure_reason: FailureReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scores", tuple(self.scores))


@dataclass(frozen=True, slots=True)
class RerankerConfig:
    model: str
    model_path: str = field(repr=False)
    device: str
    dtype: str
    candidate_limit: int  # Configuration only in M1; K/C orchestration belongs to M2.
    batch_size: int
    max_length: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        values = (self.candidate_limit, self.batch_size, self.max_length)
        if (
            self.model != MODEL_ID
            or not isinstance(self.model_path, str) or not self.model_path.strip()
            or self.device != "cuda" or self.dtype not in ("fp16", "bf16")
            or any(type(value) is not int or value <= 0 for value in values)
            or self.max_length not in (512, 1024, 2048, 4096)
            or type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
        ):
            raise RerankError("configuration_invalid")

    @classmethod
    def from_settings(cls, settings: Settings) -> RerankerConfig:
        if settings.reranker_provider != "local_transformers":
            raise RerankError("configuration_invalid")
        return cls(
            model=settings.reranker_model, model_path=settings.reranker_model_path,
            device=settings.reranker_device, dtype=settings.reranker_dtype,
            candidate_limit=settings.reranker_candidate_limit,
            batch_size=settings.reranker_batch_size,
            max_length=settings.reranker_max_length,
            timeout_seconds=settings.reranker_timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class RerankBudget:
    deadline: float
    cancelled: Event = field(default_factory=Event, repr=False, compare=False)
    token: str = field(default_factory=lambda: uuid4().hex)
    clock: Callable[[], float] = field(default=monotonic, repr=False, compare=False)

    def check(self) -> None:
        if self.cancelled.is_set() or self.clock() >= self.deadline:
            raise RerankError("timeout")


class RerankerProvider(Protocol):
    def rerank(self, request: RerankRequest, *, budget: RerankBudget) -> RerankResult: ...

    def close(self) -> None: ...


def validate_request(request: RerankRequest) -> None:
    if (
        not isinstance(request.request_id, str) or not request.request_id
        or not isinstance(request.query, str) or not request.query.strip()
    ):
        raise RerankError("invalid_request")
    identities: set[str] = set()
    for candidate in request.candidates:
        if (
            not isinstance(candidate.chunk_id, str) or not candidate.chunk_id
            or candidate.chunk_id in identities
            or type(candidate.original_rank) is not int or candidate.original_rank < 0
            or not isinstance(candidate.content, str)
        ):
            raise RerankError("invalid_request")
        identities.add(candidate.chunk_id)


def validate_result(request: RerankRequest, result: RerankResult) -> None:
    expected = {(item.chunk_id, item.original_rank) for item in request.candidates}
    actual = {(item.chunk_id, item.original_rank) for item in result.scores}
    if (
        result.request_id != request.request_id
        or len(result.scores) != len(request.candidates)
        or len(actual) != len(result.scores) or actual != expected
        or any(not math.isfinite(item.raw_score) for item in result.scores)
    ):
        raise RerankError("invalid_output")
