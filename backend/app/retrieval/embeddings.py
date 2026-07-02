from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import import_module
from math import sqrt
from pathlib import Path
from typing import Any, Protocol

from app.core.errors import (
    EMBEDDING_CONFIG_INVALID,
    EMBEDDING_DEPENDENCY_MISSING,
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_GENERATION_FAILED,
    EMBEDDING_MODEL_LOAD_FAILED,
    EMBEDDING_MODEL_PATH_NOT_FOUND,
    BusinessError,
)


@dataclass(frozen=True)
class EmbeddingResult:
    embeddings: list[list[float]]
    embedding_model: str
    embedding_dim: int
    device: str
    provider: str


class EmbeddingProvider(Protocol):
    def encode_documents(self, texts: list[str]) -> EmbeddingResult:
        ...

    def encode_query(self, query: str) -> EmbeddingResult:
        ...


class LocalQwen3EmbeddingProvider:
    provider_name = "local_qwen3"

    def __init__(
        self,
        settings: Any,
        *,
        model_loader: Callable[["LocalQwen3EmbeddingProvider"], Any] | None = None,
    ) -> None:
        self.embedding_model = settings.embedding_model
        self.embedding_dim = int(settings.embedding_dim)
        self.model_path = settings.embedding_model_path
        self.device = settings.embedding_device
        self.batch_size = int(settings.embedding_batch_size)
        self.normalize = bool(settings.embedding_normalize)
        self.local_files_only = bool(settings.embedding_local_files_only)
        self.query_instruction = settings.embedding_query_instruction
        self.use_query_instruction = bool(settings.embedding_use_query_instruction)
        self._model_loader = model_loader
        self._model: Any | None = None
        self._validate_config()

    def encode_documents(self, texts: list[str]) -> EmbeddingResult:
        return self._encode(texts, is_query=False)

    def encode_query(self, query: str) -> EmbeddingResult:
        return self._encode([query], is_query=True)

    def _validate_config(self) -> None:
        if self.batch_size <= 0:
            raise BusinessError(
                EMBEDDING_CONFIG_INVALID,
                "embedding_batch_size 必须大于 0。",
                detail={"embedding_batch_size": self.batch_size},
                status_code=400,
            )

        if self.embedding_dim <= 0:
            raise BusinessError(
                EMBEDDING_CONFIG_INVALID,
                "embedding_dim 必须大于 0。",
                detail={"embedding_dim": self.embedding_dim},
                status_code=400,
            )

    def _encode(self, texts: list[str], *, is_query: bool) -> EmbeddingResult:
        model = self._get_model()
        kwargs: dict[str, Any] = {
            "batch_size": self.batch_size,
            "normalize_embeddings": self.normalize,
        }

        if is_query and self.use_query_instruction and self.query_instruction:
            kwargs["prompt"] = self.query_instruction

        try:
            raw_embeddings = model.encode(texts, **kwargs)
            embeddings = _coerce_embedding_rows(raw_embeddings)
            _validate_embedding_dim(
                embeddings,
                expected_dim=self.embedding_dim,
                provider=self.provider_name,
                embedding_model=self.embedding_model,
            )
        except BusinessError:
            raise
        except Exception as exc:
            raise BusinessError(
                EMBEDDING_GENERATION_FAILED,
                "本地 embedding 生成失败。",
                detail={
                    "provider": self.provider_name,
                    "embedding_model": self.embedding_model,
                    "error_type": exc.__class__.__name__,
                },
                status_code=500,
            ) from exc

        return EmbeddingResult(
            embeddings=embeddings,
            embedding_model=self.embedding_model,
            embedding_dim=self.embedding_dim,
            device=self._resolved_device(),
            provider=self.provider_name,
        )

    def _get_model(self) -> Any:
        if self._model is None:
            self._model = self._load_model()
        return self._model

    def _load_model(self) -> Any:
        model_path = Path(self.model_path)
        if not model_path.exists():
            raise BusinessError(
                EMBEDDING_MODEL_PATH_NOT_FOUND,
                "本地 embedding 模型路径不存在。",
                detail={"embedding_model_path": self.model_path},
                status_code=503,
            )

        if self._model_loader is not None:
            return self._model_loader(self)

        try:
            sentence_transformers = import_module("sentence_transformers")
            sentence_transformer = sentence_transformers.SentenceTransformer
        except ImportError as exc:
            raise BusinessError(
                EMBEDDING_DEPENDENCY_MISSING,
                "缺少 sentence-transformers 依赖，无法加载本地 embedding 模型。",
                detail={"dependency": "sentence-transformers"},
                status_code=503,
            ) from exc

        kwargs: dict[str, Any] = {"local_files_only": self.local_files_only}
        if self.device != "auto":
            kwargs["device"] = self.device

        try:
            return sentence_transformer(self.model_path, **kwargs)
        except Exception as exc:
            raise BusinessError(
                EMBEDDING_MODEL_LOAD_FAILED,
                "本地 embedding 模型加载失败。",
                detail={
                    "provider": self.provider_name,
                    "embedding_model": self.embedding_model,
                    "embedding_model_path": self.model_path,
                    "local_files_only": self.local_files_only,
                    "error_type": exc.__class__.__name__,
                },
                status_code=503,
            ) from exc

    def _load_transformers_fallback(self) -> Any:
        raise BusinessError(
            EMBEDDING_MODEL_LOAD_FAILED,
            "transformers fallback 尚未实现，后续需保持本地路径和 local-only 加载。",
            detail={
                "provider": self.provider_name,
                "embedding_model": self.embedding_model,
                "embedding_model_path": self.model_path,
            },
            status_code=503,
        )

    def _resolved_device(self) -> str:
        if self.device != "auto":
            return self.device

        model = self._model
        model_device = getattr(model, "device", None)
        if model_device is not None:
            return str(model_device)

        return self.device


class HashEmbeddingProvider:
    provider_name = "hash"

    def __init__(self, settings: Any) -> None:
        self.embedding_model = getattr(settings, "embedding_model", "hash-embedding-dev")
        self.embedding_dim = int(getattr(settings, "embedding_dim", 16) or 16)
        self.device = "cpu"
        self.normalize = bool(getattr(settings, "embedding_normalize", True))
        self._validate_config()

    def encode_documents(self, texts: list[str]) -> EmbeddingResult:
        return self._build_result(texts)

    def encode_query(self, query: str) -> EmbeddingResult:
        return self._build_result([query])

    def _validate_config(self) -> None:
        if self.embedding_dim <= 0:
            raise BusinessError(
                EMBEDDING_CONFIG_INVALID,
                "embedding_dim 必须大于 0。",
                detail={"embedding_dim": self.embedding_dim},
                status_code=400,
            )

    def _build_result(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(
            embeddings=[self._embed_text(text) for text in texts],
            embedding_model=self.embedding_model,
            embedding_dim=self.embedding_dim,
            device=self.device,
            provider=self.provider_name,
        )

    def _embed_text(self, text: str) -> list[float]:
        vector = [0.0 for _ in range(self.embedding_dim)]
        tokens = list(text) if text else [""]

        for index, token in enumerate(tokens):
            digest = hashlib.sha256(f"{index}:{token}".encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.embedding_dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 0.5 + (digest[5] / 255.0)
            vector[bucket] += sign * weight

        if self.normalize:
            return _normalize_vector(vector)

        return vector


def get_embedding_provider(settings: Any) -> EmbeddingProvider:
    provider = settings.embedding_provider.strip().lower()

    if provider == "local_qwen3":
        return LocalQwen3EmbeddingProvider(settings)

    if provider == "hash":
        return HashEmbeddingProvider(settings)

    raise BusinessError(
        EMBEDDING_CONFIG_INVALID,
        "未支持的 embedding provider 配置。",
        detail={"embedding_provider": settings.embedding_provider},
        status_code=400,
    )


def _coerce_embedding_rows(raw_embeddings: Any) -> list[list[float]]:
    if hasattr(raw_embeddings, "tolist"):
        raw_embeddings = raw_embeddings.tolist()

    if raw_embeddings is None:
        return []

    rows = list(raw_embeddings)
    if not rows:
        return []

    first = rows[0]
    if isinstance(first, int | float):
        return [[float(value) for value in rows]]

    coerced_rows: list[list[float]] = []
    for row in rows:
        if hasattr(row, "tolist"):
            row = row.tolist()
        coerced_rows.append([float(value) for value in row])

    return coerced_rows


def _validate_embedding_dim(
    embeddings: list[list[float]],
    *,
    expected_dim: int,
    provider: str,
    embedding_model: str,
) -> None:
    for index, embedding in enumerate(embeddings):
        actual_dim = len(embedding)
        if actual_dim != expected_dim:
            raise BusinessError(
                EMBEDDING_DIMENSION_MISMATCH,
                "embedding 输出维度与配置不一致。",
                detail={
                    "provider": provider,
                    "embedding_model": embedding_model,
                    "index": index,
                    "expected_dim": expected_dim,
                    "actual_dim": actual_dim,
                },
                status_code=500,
            )


def _normalize_vector(vector: Sequence[float]) -> list[float]:
    norm = sqrt(sum(value * value for value in vector))
    if norm == 0:
        return [float(value) for value in vector]
    return [float(value) / norm for value in vector]
