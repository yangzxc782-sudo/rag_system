from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from fastapi.testclient import TestClient

from app.api.v1 import search as search_api
from app.core.errors import (
    DOCUMENT_NOT_FOUND,
    EMBEDDING_CONFIG_INVALID,
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_GENERATION_FAILED,
    BusinessError,
)
from app.main import app
from app.retrieval.embeddings import EmbeddingResult
from app.services import vector_search as vector_search_service
from app.services.vector_search import VectorSearchItem, VectorSearchResult


client = TestClient(app)

DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
CHUNK_ID = UUID("660e8400-e29b-41d4-a716-446655440001")
OTHER_CHUNK_ID = UUID("660e8400-e29b-41d4-a716-446655440002")


class FakeDb:
    def __init__(self, *, document=None) -> None:
        self.document = document
        self.executed = []

    def get(self, model, item_id):
        return self.document

    def execute(self, statement):
        self.executed.append(statement)
        raise AssertionError("tests should monkeypatch _query_vector_rows")


class FakeProvider:
    def __init__(self, *, dim: int = 1024, raises: Exception | None = None) -> None:
        self.dim = dim
        self.raises = raises
        self.query_calls: list[str] = []
        self.document_calls: list[list[str]] = []

    def encode_query(self, query: str) -> EmbeddingResult:
        self.query_calls.append(query)
        if self.raises is not None:
            raise self.raises
        return EmbeddingResult(
            embeddings=[[0.1 for _ in range(self.dim)]],
            embedding_model="Qwen3-Embedding-0.6B",
            embedding_dim=self.dim,
            device="cpu",
            provider="fake",
        )

    def encode_documents(self, texts: list[str]) -> EmbeddingResult:
        self.document_calls.append(texts)
        raise AssertionError("vector search must use encode_query")


def fake_settings(*, dim: int = 1024) -> SimpleNamespace:
    return SimpleNamespace(
        embedding_provider="local_qwen3",
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_model_path="D:/rag_system/models/Qwen3-Embedding-0.6B",
        embedding_device="auto",
        embedding_batch_size=8,
        embedding_normalize=True,
        embedding_dim=dim,
        embedding_local_files_only=True,
        embedding_query_instruction="query instruction",
        embedding_use_query_instruction=True,
    )


def fake_row(
    *,
    chunk_id=CHUNK_ID,
    document_id=DOCUMENT_ID,
    chunk_index: int = 0,
    status: str = "embedded",
    distance: float = 0.2,
    embedding=object(),
) -> dict:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "original_filename": "casting-notes.txt",
        "chunk_index": chunk_index,
        "content": "Riser design should feed hot spots effectively.",
        "chunk_type": "text",
        "source_metadata": {"parser_name": "simple", "character_count": 42},
        "embedding_model": "Qwen3-Embedding-0.6B",
        "embedding_dim": 1024,
        "embedding_status": status,
        "embedding": embedding,
        "distance": distance,
    }


def patch_vector_search(monkeypatch, *, provider=None, rows=None, settings=None):
    provider = provider or FakeProvider()
    rows = rows or []
    captured = {}

    def fake_query_vector_rows(db, *, query_embedding, embedding_model, embedding_dim, limit, document_id):
        captured["query_embedding"] = query_embedding
        captured["embedding_model"] = embedding_model
        captured["embedding_dim"] = embedding_dim
        captured["limit"] = limit
        captured["document_id"] = document_id
        return rows

    monkeypatch.setattr(vector_search_service, "get_settings", lambda: settings or fake_settings())
    monkeypatch.setattr(vector_search_service, "get_embedding_provider", lambda current_settings: provider)
    monkeypatch.setattr(vector_search_service, "_query_vector_rows", fake_query_vector_rows)
    return provider, captured


def test_vector_search_empty_query_returns_error() -> None:
    db = FakeDb(document=object())

    with pytest.raises(BusinessError) as exc_info:
        vector_search_service.vector_search_chunks(db, query="   ")

    assert exc_info.value.code == EMBEDDING_CONFIG_INVALID


def test_vector_search_no_embedded_chunks_returns_empty(monkeypatch) -> None:
    provider, captured = patch_vector_search(monkeypatch, rows=[])
    db = FakeDb(document=object())

    result = vector_search_service.vector_search_chunks(db, query="riser design", limit=10)

    assert result.query == "riser design"
    assert result.limit == 10
    assert result.total == 0
    assert result.items == []
    assert provider.query_calls == ["riser design"]
    assert provider.document_calls == []
    assert len(captured["query_embedding"]) == 1024


def test_vector_search_filters_to_embedded_rows_with_embedding(monkeypatch) -> None:
    rows = [
        fake_row(chunk_id=CHUNK_ID, status="embedded", distance=0.1, embedding=[0.1]),
        fake_row(chunk_id=OTHER_CHUNK_ID, status="not_started", distance=0.01, embedding=[0.1]),
        fake_row(status="embed_failed", distance=0.02, embedding=[0.1]),
        fake_row(status="embedding", distance=0.03, embedding=[0.1]),
        fake_row(status="embedded", distance=0.04, embedding=None),
        {**fake_row(status="embedded", distance=0.05, embedding=[0.1]), "embedding_model": "other-model"},
        {**fake_row(status="embedded", distance=0.06, embedding=[0.1]), "embedding_dim": 3},
    ]
    patch_vector_search(monkeypatch, rows=rows)
    db = FakeDb(document=object())

    result = vector_search_service.vector_search_chunks(db, query="hot spot")

    assert result.total == 1
    assert result.items[0].chunk_id == CHUNK_ID
    assert result.items[0].embedding_status == "embedded"


def test_vector_search_document_id_filter_is_passed(monkeypatch) -> None:
    _, captured = patch_vector_search(monkeypatch, rows=[fake_row(document_id=DOCUMENT_ID)])
    db = FakeDb(document=object())

    result = vector_search_service.vector_search_chunks(db, query="feeding", document_id=DOCUMENT_ID)

    assert captured["document_id"] == DOCUMENT_ID
    assert captured["embedding_model"] == "Qwen3-Embedding-0.6B"
    assert captured["embedding_dim"] == 1024
    assert result.document_id == DOCUMENT_ID
    assert result.total == 1


def test_vector_query_filters_document_deletion_status_normal() -> None:
    class EmptyMappings:
        def mappings(self):
            return self

        def all(self):
            return []

    class CaptureDb:
        def __init__(self) -> None:
            self.statement = None

        def execute(self, statement):
            self.statement = statement
            return EmptyMappings()

    db = CaptureDb()
    vector_search_service._query_vector_rows(
        db,
        query_embedding=[0.1] * 1024,
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_dim=1024,
        limit=10,
        document_id=None,
    )

    sql = str(
        db.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "documents.deletion_status = 'normal'" in sql


def test_vector_search_document_id_not_found() -> None:
    db = FakeDb(document=None)

    with pytest.raises(BusinessError) as exc_info:
        vector_search_service.vector_search_chunks(db, query="feeding", document_id=DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_NOT_FOUND


def test_vector_search_uses_encode_query_not_encode_documents(monkeypatch) -> None:
    provider, _ = patch_vector_search(monkeypatch, rows=[fake_row()])
    db = FakeDb(document=object())

    vector_search_service.vector_search_chunks(db, query="molding sand strength")

    assert provider.query_calls == ["molding sand strength"]
    assert provider.document_calls == []


def test_vector_search_dimension_mismatch(monkeypatch) -> None:
    patch_vector_search(monkeypatch, provider=FakeProvider(dim=3), rows=[fake_row()])
    db = FakeDb(document=object())

    with pytest.raises(BusinessError) as exc_info:
        vector_search_service.vector_search_chunks(db, query="dimension tolerance")

    assert exc_info.value.code == EMBEDDING_DIMENSION_MISMATCH


def test_vector_search_orders_by_distance_and_computes_score(monkeypatch) -> None:
    rows = [
        fake_row(chunk_id=OTHER_CHUNK_ID, chunk_index=1, distance=0.4),
        fake_row(chunk_id=CHUNK_ID, chunk_index=0, distance=0.1),
    ]
    patch_vector_search(monkeypatch, rows=rows)
    db = FakeDb(document=object())

    result = vector_search_service.vector_search_chunks(db, query="pouring temperature")

    assert [item.distance for item in result.items] == [0.1, 0.4]
    assert result.items[0].score == pytest.approx(0.9)
    assert result.items[1].score == pytest.approx(0.6)


def test_vector_search_does_not_write_retrieval_logs(monkeypatch) -> None:
    patch_vector_search(monkeypatch, rows=[fake_row()])
    db = FakeDb(document=object())

    vector_search_service.vector_search_chunks(db, query="shrinkage cavity")

    assert not hasattr(db, "retrieval_logs")


def fake_vector_search_result() -> VectorSearchResult:
    return VectorSearchResult(
        query="riser design",
        limit=5,
        document_id=DOCUMENT_ID,
        total=1,
        items=[
            VectorSearchItem(
                chunk_id=CHUNK_ID,
                document_id=DOCUMENT_ID,
                original_filename="casting-notes.txt",
                chunk_index=0,
                content="Riser design should feed hot spots effectively.",
                chunk_type="text",
                source_metadata={"parser_name": "simple"},
                embedding_model="Qwen3-Embedding-0.6B",
                embedding_dim=1024,
                embedding_status="embedded",
                distance=0.12,
                score=0.88,
            )
        ],
    )


def test_vector_search_api_success(monkeypatch) -> None:
    def fake_vector_search_chunks(db, *, query, limit, document_id):
        assert query == "riser design"
        assert limit == 5
        assert document_id == DOCUMENT_ID
        return fake_vector_search_result()

    monkeypatch.setattr(search_api, "vector_search_chunks", fake_vector_search_chunks)

    response = client.post(
        "/api/v1/search/vector",
        json={"query": "riser design", "limit": 5, "document_id": str(DOCUMENT_ID)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["query"] == "riser design"
    assert body["data"]["limit"] == 5
    assert body["data"]["total"] == 1
    item = body["data"]["items"][0]
    assert item["chunk_id"] == str(CHUNK_ID)
    assert item["document_id"] == str(DOCUMENT_ID)
    assert item["original_filename"] == "casting-notes.txt"
    assert item["chunk_index"] == 0
    assert item["content"] == "Riser design should feed hot spots effectively."
    assert item["distance"] == 0.12
    assert item["score"] == 0.88
    assert item["source_metadata"] == {"parser_name": "simple"}


def test_vector_search_api_empty_query_returns_400(monkeypatch) -> None:
    def fake_vector_search_chunks(db, *, query, limit, document_id):
        raise BusinessError(EMBEDDING_CONFIG_INVALID, "empty query", status_code=400)

    monkeypatch.setattr(search_api, "vector_search_chunks", fake_vector_search_chunks)

    response = client.post("/api/v1/search/vector", json={"query": "   "})

    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == EMBEDDING_CONFIG_INVALID


def test_vector_search_api_provider_failure_returns_error(monkeypatch) -> None:
    def fake_vector_search_chunks(db, *, query, limit, document_id):
        raise BusinessError(EMBEDDING_GENERATION_FAILED, "provider failed", status_code=500)

    monkeypatch.setattr(search_api, "vector_search_chunks", fake_vector_search_chunks)

    response = client.post("/api/v1/search/vector", json={"query": "riser design"})

    assert response.status_code == 500
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == EMBEDDING_GENERATION_FAILED
