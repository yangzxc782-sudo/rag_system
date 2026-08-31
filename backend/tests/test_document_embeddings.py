from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.core.errors import (
    DOCUMENT_EMBEDDINGS_ALREADY_GENERATED,
    DOCUMENT_NOT_FOUND,
    DOCUMENT_NOT_PARSED,
    DOCUMENT_DELETE_FAILED,
    EMBEDDING_CONFIG_INVALID,
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_GENERATION_FAILED,
    BusinessError,
)
from app.retrieval.embeddings import EmbeddingResult
from app.services import embeddings as embedding_service


DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


class FakeScalarResult:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class FakeDb:
    def __init__(self, *, document=None, chunks=None) -> None:
        self.document = document
        self.chunks = chunks or []
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def get(self, model, item_id):
        return self.document

    def scalars(self, statement):
        return FakeScalarResult(sorted(self.chunks, key=lambda chunk: chunk.chunk_index))

    def scalar(self, statement):
        if "FROM documents" in str(statement):
            return self.document
        return None

    def add(self, item):
        self.added.append(item)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeProvider:
    def __init__(self, *, dim: int = 1024, raises: Exception | None = None) -> None:
        self.dim = dim
        self.raises = raises
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    def encode_documents(self, texts: list[str]) -> EmbeddingResult:
        self.document_calls.append(texts)
        if self.raises is not None:
            raise self.raises
        return EmbeddingResult(
            embeddings=[[float(index + 1) for index in range(self.dim)] for _ in texts],
            embedding_model="fake-qwen",
            embedding_dim=self.dim,
            device="cpu",
            provider="fake",
        )

    def encode_query(self, query: str) -> EmbeddingResult:
        self.query_calls.append(query)
        return EmbeddingResult(
            embeddings=[[0.0 for _ in range(self.dim)]],
            embedding_model="fake-qwen",
            embedding_dim=self.dim,
            device="cpu",
            provider="fake",
        )


def fake_document() -> SimpleNamespace:
    return SimpleNamespace(id=DOCUMENT_ID, deletion_status="normal")


def fake_chunk(
    *,
    index: int,
    status: str = "not_started",
    content: str = "chunk content",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        document_id=DOCUMENT_ID,
        chunk_index=index,
        content=content,
        embedding=None,
        embedding_model=None,
        embedding_dim=None,
        embedding_status=status,
        embedding_error_message=None,
        embedding_updated_at=None,
        source_metadata={"character_count": len(content)},
    )


def fake_settings(*, batch_size: int = 8, dim: int = 1024) -> SimpleNamespace:
    return SimpleNamespace(
        embedding_provider="local_qwen3",
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_model_path="D:/rag_system/models/Qwen3-Embedding-0.6B",
        embedding_device="auto",
        embedding_batch_size=batch_size,
        embedding_normalize=True,
        embedding_dim=dim,
        embedding_local_files_only=True,
        embedding_query_instruction="query instruction",
        embedding_use_query_instruction=True,
    )


def patch_provider(monkeypatch, provider: FakeProvider, *, settings=None) -> FakeProvider:
    monkeypatch.setattr(embedding_service, "get_settings", lambda: settings or fake_settings())
    monkeypatch.setattr(embedding_service, "get_embedding_provider", lambda current_settings: provider)
    return provider


def test_embedding_delete_failed_document_never_calls_provider(monkeypatch) -> None:
    document = fake_document()
    document.deletion_status = "delete_failed"
    chunks = [fake_chunk(index=0)]
    db = FakeDb(document=document, chunks=chunks)
    provider = patch_provider(monkeypatch, FakeProvider())

    with pytest.raises(BusinessError) as exc_info:
        embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_DELETE_FAILED
    assert provider.document_calls == []
    assert chunks[0].embedding is None


def test_embedding_final_guard_blocks_vectors_after_delete_commits(monkeypatch) -> None:
    document = fake_document()
    chunks = [fake_chunk(index=0)]
    db = FakeDb(document=document, chunks=chunks)

    class DeleteDuringInference(FakeProvider):
        def encode_documents(self, texts):
            result = super().encode_documents(texts)
            document.deletion_status = "deleting"
            return result

    patch_provider(monkeypatch, DeleteDuringInference())

    with pytest.raises(BusinessError) as exc_info:
        embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert exc_info.value.code == "DOCUMENT_DELETION_IN_PROGRESS"
    assert chunks[0].embedding is None


def test_generate_document_embeddings_document_not_found() -> None:
    db = FakeDb(document=None)

    with pytest.raises(BusinessError) as exc_info:
        embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_NOT_FOUND


def test_generate_document_embeddings_document_not_parsed() -> None:
    db = FakeDb(document=fake_document(), chunks=[])

    with pytest.raises(BusinessError) as exc_info:
        embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_NOT_PARSED


def test_generate_document_embeddings_not_started_success(monkeypatch) -> None:
    provider = patch_provider(monkeypatch, FakeProvider())
    chunks = [fake_chunk(index=0), fake_chunk(index=1)]
    db = FakeDb(document=fake_document(), chunks=chunks)

    result = embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert result.document_id == DOCUMENT_ID
    assert result.total == 2
    assert result.embedded == 2
    assert result.skipped == 0
    assert result.failed == 0
    assert result.model == "fake-qwen"
    assert result.dim == 1024
    assert result.device == "cpu"
    assert provider.document_calls == [["chunk content", "chunk content"]]
    assert provider.query_calls == []
    for chunk in chunks:
        assert chunk.embedding_status == "embedded"
        assert chunk.embedding is not None
        assert len(chunk.embedding) == 1024
        assert chunk.embedding_model == "fake-qwen"
        assert chunk.embedding_dim == 1024
        assert chunk.embedding_error_message is None
        assert chunk.embedding_updated_at is not None


def test_generate_document_embeddings_embed_failed_can_regenerate(monkeypatch) -> None:
    patch_provider(monkeypatch, FakeProvider())
    chunk = fake_chunk(index=0, status="embed_failed")
    chunk.embedding_error_message = "previous failure"
    db = FakeDb(document=fake_document(), chunks=[chunk])

    result = embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert result.embedded == 1
    assert chunk.embedding_status == "embedded"
    assert chunk.embedding_error_message is None


def test_generate_document_embeddings_batches_by_config(monkeypatch) -> None:
    provider = patch_provider(monkeypatch, FakeProvider(), settings=fake_settings(batch_size=1))
    chunks = [fake_chunk(index=0, content="first"), fake_chunk(index=1, content="second")]
    db = FakeDb(document=fake_document(), chunks=chunks)

    result = embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert result.embedded == 2
    assert provider.document_calls == [["first"], ["second"]]


def test_generate_document_embeddings_invalid_batch_size_raises(monkeypatch) -> None:
    patch_provider(monkeypatch, FakeProvider(), settings=fake_settings(batch_size=0))
    db = FakeDb(document=fake_document(), chunks=[fake_chunk(index=0)])

    with pytest.raises(BusinessError) as exc_info:
        embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert exc_info.value.code == EMBEDDING_CONFIG_INVALID


def test_generate_document_embeddings_skips_embedded_chunks(monkeypatch) -> None:
    provider = patch_provider(monkeypatch, FakeProvider())
    embedded_chunk = fake_chunk(index=0, status="embedded")
    embedded_chunk.embedding = [9.0]
    embedded_chunk.embedding_model = "existing"
    embedded_chunk.embedding_dim = 1
    target_chunk = fake_chunk(index=1, status="not_started", content="new chunk")
    db = FakeDb(document=fake_document(), chunks=[embedded_chunk, target_chunk])

    result = embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert result.total == 2
    assert result.embedded == 1
    assert result.skipped == 1
    assert embedded_chunk.embedding == [9.0]
    assert embedded_chunk.embedding_model == "existing"
    assert embedded_chunk.embedding_dim == 1
    assert target_chunk.embedding_status == "embedded"
    assert provider.document_calls == [["new chunk"]]


def test_generate_document_embeddings_all_embedded_raises() -> None:
    chunks = [fake_chunk(index=0, status="embedded"), fake_chunk(index=1, status="embedded")]
    db = FakeDb(document=fake_document(), chunks=chunks)

    with pytest.raises(BusinessError) as exc_info:
        embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_EMBEDDINGS_ALREADY_GENERATED


def test_generate_document_embeddings_dimension_mismatch_marks_failed(monkeypatch) -> None:
    patch_provider(monkeypatch, FakeProvider(dim=3))
    chunk = fake_chunk(index=0)
    db = FakeDb(document=fake_document(), chunks=[chunk])

    with pytest.raises(BusinessError) as exc_info:
        embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert exc_info.value.code == EMBEDDING_DIMENSION_MISMATCH
    assert chunk.embedding_status == "embed_failed"
    assert chunk.embedding_error_message


def test_generate_document_embeddings_provider_error_marks_failed(monkeypatch) -> None:
    provider_error = BusinessError(
        EMBEDDING_GENERATION_FAILED,
        "provider failed",
        status_code=500,
    )
    patch_provider(monkeypatch, FakeProvider(raises=provider_error))
    chunk = fake_chunk(index=0)
    db = FakeDb(document=fake_document(), chunks=[chunk])

    with pytest.raises(BusinessError) as exc_info:
        embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert exc_info.value.code == EMBEDDING_GENERATION_FAILED
    assert chunk.embedding_status == "embed_failed"
    assert chunk.embedding_error_message == "provider failed"
    assert chunk.embedding_updated_at is not None


def test_get_document_embedding_status_returns_counts() -> None:
    chunks = [
        fake_chunk(index=0, status="not_started"),
        fake_chunk(index=1, status="embedding"),
        fake_chunk(index=2, status="embedded"),
        fake_chunk(index=3, status="embed_failed"),
    ]
    chunks[2].embedding_model = "fake-qwen"
    chunks[2].embedding_dim = 1024
    chunks[3].embedding_model = "fake-qwen"
    chunks[3].embedding_dim = 1024
    db = FakeDb(document=fake_document(), chunks=chunks)

    result = embedding_service.get_document_embedding_status(db, DOCUMENT_ID)

    assert result.document_id == DOCUMENT_ID
    assert result.total == 4
    assert result.not_started == 1
    assert result.embedding == 1
    assert result.embedded == 1
    assert result.embed_failed == 1
    assert result.models == ["fake-qwen"]
    assert result.dims == [1024]


def test_generate_document_embeddings_does_not_create_retrieval_logs(monkeypatch) -> None:
    patch_provider(monkeypatch, FakeProvider())
    db = FakeDb(document=fake_document(), chunks=[fake_chunk(index=0)])

    embedding_service.generate_document_embeddings(db, DOCUMENT_ID)

    assert not hasattr(db, "retrieval_logs")
