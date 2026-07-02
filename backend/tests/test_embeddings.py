from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.errors import (
    EMBEDDING_CONFIG_INVALID,
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_MODEL_PATH_NOT_FOUND,
    BusinessError,
)
from app.retrieval.embeddings import (
    HashEmbeddingProvider,
    LocalQwen3EmbeddingProvider,
    get_embedding_provider,
)


def fake_settings(**overrides):
    settings = {
        "embedding_provider": "hash",
        "embedding_model": "Qwen3-Embedding-0.6B",
        "embedding_model_path": "D:/rag_system/models/Qwen3-Embedding-0.6B",
        "embedding_device": "auto",
        "embedding_batch_size": 8,
        "embedding_normalize": True,
        "embedding_dim": 3,
        "embedding_local_files_only": True,
        "embedding_query_instruction": "query instruction",
        "embedding_use_query_instruction": True,
    }
    settings.update(overrides)
    return SimpleNamespace(**settings)


class FakeModel:
    device = "cpu"

    def __init__(self, *, dim: int = 3) -> None:
        self.dim = dim
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append({"texts": list(texts), "kwargs": kwargs})
        return [[float(index + 1) for index in range(self.dim)] for _ in texts]


def test_hash_embedding_provider_is_deterministic() -> None:
    provider = HashEmbeddingProvider(fake_settings(embedding_dim=8))

    first = provider.encode_documents(["铸型工艺"]).embeddings
    second = provider.encode_documents(["铸型工艺"]).embeddings

    assert first == second


def test_hash_embedding_provider_uses_configured_dim() -> None:
    provider = HashEmbeddingProvider(fake_settings(embedding_dim=5))

    result = provider.encode_documents(["content"])

    assert result.embedding_dim == 5
    assert len(result.embeddings[0]) == 5


def test_hash_embedding_provider_normalizes_vectors() -> None:
    provider = HashEmbeddingProvider(fake_settings(embedding_dim=8, embedding_normalize=True))

    vector = provider.encode_documents(["content"]).embeddings[0]
    norm = sum(value * value for value in vector) ** 0.5

    assert norm == pytest.approx(1.0)


def test_hash_embedding_provider_supports_documents_and_query() -> None:
    provider = HashEmbeddingProvider(fake_settings(embedding_dim=4))

    document_result = provider.encode_documents(["a", "b"])
    query_result = provider.encode_query("a")

    assert len(document_result.embeddings) == 2
    assert len(query_result.embeddings) == 1
    assert document_result.provider == "hash"
    assert query_result.provider == "hash"


def test_get_embedding_provider_returns_hash_provider() -> None:
    provider = get_embedding_provider(fake_settings(embedding_provider="hash"))

    assert isinstance(provider, HashEmbeddingProvider)


def test_get_embedding_provider_returns_local_qwen3_without_loading_model() -> None:
    provider = get_embedding_provider(fake_settings(embedding_provider="local_qwen3"))

    assert isinstance(provider, LocalQwen3EmbeddingProvider)
    assert provider._model is None


def test_get_embedding_provider_rejects_unknown_provider() -> None:
    with pytest.raises(BusinessError) as exc_info:
        get_embedding_provider(fake_settings(embedding_provider="unknown"))

    assert exc_info.value.code == EMBEDDING_CONFIG_INVALID


def test_local_qwen3_provider_initialization_does_not_load_model(tmp_path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    load_calls = 0

    def fake_loader(provider):
        nonlocal load_calls
        load_calls += 1
        return FakeModel()

    provider = LocalQwen3EmbeddingProvider(
        fake_settings(embedding_provider="local_qwen3", embedding_model_path=str(model_dir)),
        model_loader=fake_loader,
    )

    assert provider._model is None
    assert load_calls == 0


def test_local_qwen3_provider_missing_model_path_raises_on_encode(tmp_path) -> None:
    missing_path = tmp_path / "missing-model"
    provider = LocalQwen3EmbeddingProvider(
        fake_settings(embedding_provider="local_qwen3", embedding_model_path=str(missing_path)),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.encode_documents(["content"])

    assert exc_info.value.code == EMBEDDING_MODEL_PATH_NOT_FOUND


def test_local_qwen3_provider_rejects_invalid_batch_size() -> None:
    with pytest.raises(BusinessError) as exc_info:
        LocalQwen3EmbeddingProvider(fake_settings(embedding_provider="local_qwen3", embedding_batch_size=0))

    assert exc_info.value.code == EMBEDDING_CONFIG_INVALID


def test_local_qwen3_provider_rejects_invalid_embedding_dim() -> None:
    with pytest.raises(BusinessError) as exc_info:
        LocalQwen3EmbeddingProvider(fake_settings(embedding_provider="local_qwen3", embedding_dim=0))

    assert exc_info.value.code == EMBEDDING_CONFIG_INVALID


def test_local_qwen3_provider_encode_documents_with_fake_model(tmp_path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    fake_model = FakeModel(dim=3)
    provider = LocalQwen3EmbeddingProvider(
        fake_settings(embedding_provider="local_qwen3", embedding_model_path=str(model_dir), embedding_dim=3),
        model_loader=lambda provider: fake_model,
    )

    result = provider.encode_documents(["doc one", "doc two"])

    assert len(result.embeddings) == 2
    assert result.embedding_dim == 3
    assert result.provider == "local_qwen3"
    assert fake_model.calls[0]["texts"] == ["doc one", "doc two"]
    assert "prompt" not in fake_model.calls[0]["kwargs"]


def test_local_qwen3_provider_encode_query_with_fake_model(tmp_path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    fake_model = FakeModel(dim=3)
    provider = LocalQwen3EmbeddingProvider(
        fake_settings(embedding_provider="local_qwen3", embedding_model_path=str(model_dir), embedding_dim=3),
        model_loader=lambda provider: fake_model,
    )

    result = provider.encode_query("query text")

    assert len(result.embeddings) == 1
    assert result.embedding_dim == 3
    assert fake_model.calls[0]["texts"] == ["query text"]
    assert fake_model.calls[0]["kwargs"]["prompt"] == "query instruction"


def test_local_qwen3_provider_dimension_mismatch_raises(tmp_path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    provider = LocalQwen3EmbeddingProvider(
        fake_settings(embedding_provider="local_qwen3", embedding_model_path=str(model_dir), embedding_dim=4),
        model_loader=lambda provider: FakeModel(dim=3),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.encode_documents(["content"])

    assert exc_info.value.code == EMBEDDING_DIMENSION_MISMATCH


def test_query_instruction_only_applies_to_query_encoding(tmp_path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    fake_model = FakeModel(dim=3)
    provider = LocalQwen3EmbeddingProvider(
        fake_settings(embedding_provider="local_qwen3", embedding_model_path=str(model_dir), embedding_dim=3),
        model_loader=lambda provider: fake_model,
    )

    provider.encode_documents(["document"])
    provider.encode_query("query")

    assert "prompt" not in fake_model.calls[0]["kwargs"]
    assert fake_model.calls[1]["kwargs"]["prompt"] == "query instruction"
