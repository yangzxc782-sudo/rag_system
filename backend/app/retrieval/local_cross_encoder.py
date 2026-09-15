"""Local-only Transformers scoring. Scheduling and final ordering live in the service."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import math
from pathlib import Path
from threading import Lock
from traceback import clear_frames
from typing import Any, Callable

from app.retrieval.reranker import (
    RerankBudget, RerankCandidate, RerankError, RerankRequest, RerankResult, RerankScore,
    RerankerConfig, validate_request, validate_result,
)


@dataclass(slots=True, repr=False)
class LoadedCrossEncoder:
    tokenizer: Any
    model: Any
    torch: Any


def _load_local_model(config: RerankerConfig) -> LoadedCrossEncoder:
    # Imports are deliberately inside the first admitted task, never at startup.
    torch = import_module("torch")
    if not torch.cuda.is_available():
        raise RerankError("unavailable")
    if config.dtype == "bf16" and not torch.cuda.is_bf16_supported():
        raise RerankError("unavailable")
    transformers = import_module("transformers")
    path = str(Path(config.model_path).resolve())
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        path, local_files_only=True, trust_remote_code=False, use_fast=True,
    )
    model = transformers.AutoModelForSequenceClassification.from_pretrained(
        path, local_files_only=True, trust_remote_code=False, use_safetensors=True,
        dtype=torch.float16 if config.dtype == "fp16" else torch.bfloat16,
    )
    return LoadedCrossEncoder(tokenizer, model, torch)


class LocalCrossEncoderProvider:
    """One runtime owned by one service worker, with injectable loading for unit tests.

    Load failures are latched until application restart (one attempt per instance).
    Ordinary inference/OOM failures may reuse a healthy CUDA runtime next request.
    An unsuccessful CUDA synchronization latches unavailable until restart.
    """

    def __init__(
        self, config: RerankerConfig, *,
        model_loader: Callable[[RerankerConfig], LoadedCrossEncoder] | None = None,
    ) -> None:
        self.config = config
        self._model_loader = model_loader or _load_local_model
        self._loaded: LoadedCrossEncoder | None = None
        self._lock = Lock()
        self._closed = False
        self._unavailable = False

    def rerank(self, request: RerankRequest, *, budget: RerankBudget) -> RerankResult:
        if not self._lock.acquire(blocking=False):
            return RerankResult(request.request_id, failure_reason="busy")
        try:
            if self._closed:
                raise RerankError("closed")
            if self._unavailable:
                raise RerankError("unavailable")
            validate_request(request)
            budget.check()
            if not request.candidates:
                return RerankResult(request.request_id)
            self._ensure_loaded()
            budget.check()
            scores: list[RerankScore] = []
            for start in range(0, len(request.candidates), self.config.batch_size):
                budget.check()
                batch = request.candidates[start:start + self.config.batch_size]
                raw_scores = self._score_batch(request.query, batch, budget)
                budget.check()
                scores.extend(RerankScore(item.chunk_id, item.original_rank, score)
                              for item, score in zip(batch, raw_scores, strict=True))
            result = RerankResult(request.request_id, tuple(scores))
            validate_result(request, result)
            return result
        except RerankError as exc:
            return RerankResult(request.request_id, failure_reason=exc.reason)
        except Exception as exc:
            loaded = self._loaded
            is_oom = loaded is not None and isinstance(exc, loaded.torch.cuda.OutOfMemoryError)
            # Model forward traceback frames can retain activations even after our input
            # references are dropped. Release those before OOM cache cleanup, without GC
            # of other providers or unloading Embedding.
            clear_frames(exc.__traceback__)
            # Synchronize after errors too: busy cannot be released with GPU work pending.
            if loaded is not None:
                try:
                    loaded.torch.cuda.synchronize(self.config.device)
                    if is_oom:
                        loaded.torch.cuda.empty_cache()
                except Exception:
                    self._unavailable = True
            return RerankResult(request.request_id, failure_reason="oom" if is_oom else "inference_exception")
        finally:
            self._lock.release()

    def _ensure_loaded(self) -> None:
        if self._loaded is not None:
            return
        # Latch before entering an injectable/third-party loader, including partial load failure.
        self._unavailable = True
        loaded = None
        try:
            if not Path(self.config.model_path).is_dir():
                raise RerankError("unavailable")
            loaded = self._model_loader(self.config)
            loaded.model.to(self.config.device)
            loaded.model.eval()
            loaded.torch.cuda.synchronize(self.config.device)
            self._loaded = loaded
            self._unavailable = False
        except Exception as exc:
            clear_frames(exc.__traceback__)
            if loaded is not None:
                try:
                    loaded.torch.cuda.synchronize(self.config.device)
                except Exception:
                    pass  # Already terminal unavailable; never admit another model load.
            loaded = None
            raise RerankError("unavailable") from None

    def _score_batch(
        self, query: str, candidates: tuple[RerankCandidate, ...], budget: RerankBudget,
    ) -> list[float]:
        loaded = self._loaded
        assert loaded is not None
        tokenizer = loaded.tokenizer
        query_ids = tokenizer.encode(query, add_special_tokens=False, truncation=False)
        if len(query_ids) + tokenizer.num_special_tokens_to_add(pair=True) >= self.config.max_length:
            raise RerankError("query_too_long")
        inputs = tokenizer(
            [query] * len(candidates), [item.content for item in candidates],
            padding=True, truncation="only_second", max_length=self.config.max_length,
            return_tensors="pt",
        )
        rows = inputs["input_ids"].tolist()
        if len(rows) != len(candidates):
            raise RerankError("invalid_output")
        for index, row in enumerate(rows):
            sequence_ids = inputs.sequence_ids(index)
            if len(row) > self.config.max_length or len(sequence_ids) != len(row):
                raise RerankError("invalid_output")
            if [token for token, seq in zip(row, sequence_ids) if seq == 0] != query_ids:
                raise RerankError("query_truncated")
        budget.check()
        logits = None
        try:
            with loaded.torch.inference_mode():
                inputs = inputs.to(self.config.device)
                budget.check()
                logits = loaded.model(**inputs, return_dict=True).logits
                loaded.torch.cuda.synchronize(self.config.device)
                if logits.ndim != 2 or tuple(logits.shape) != (len(candidates), 1):
                    raise RerankError("invalid_output")
                values = logits[:, 0].detach().float().cpu().tolist()
            if len(values) != len(candidates) or any(not math.isfinite(value) for value in values):
                raise RerankError("invalid_output")
            return values
        finally:
            # No tensors escape into outcomes or the service; errors discard the whole batch.
            del logits, inputs

    def close(self) -> None:
        # Service shutdown joins the worker first. Standalone callers also wait safely here.
        with self._lock:
            self._closed = True
            self._loaded = None
