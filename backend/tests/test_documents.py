from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from app.api.v1 import documents as documents_api
from app.core.errors import (
    DOCUMENT_ALREADY_PARSED,
    DOCUMENT_CHUNK_CONFIG_INVALID,
    DOCUMENT_EMBEDDINGS_ALREADY_GENERATED,
    DOCUMENT_NOT_FOUND,
    DOCUMENT_NOT_PARSED,
    DOCUMENT_PARSER_UNAVAILABLE,
    EMBEDDING_GENERATION_FAILED,
    FILE_TOO_LARGE,
    INVALID_FILE_TYPE,
    BusinessError,
)
from app.main import app


client = TestClient(app)


DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
CHUNK_ID = UUID("660e8400-e29b-41d4-a716-446655440001")


def fake_document() -> SimpleNamespace:
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=DOCUMENT_ID,
        original_filename="test.pdf",
        bucket_name="rag-documents",
        object_key=f"raw/2026/06/{DOCUMENT_ID}.pdf",
        file_type=".pdf",
        mime_type="application/pdf",
        file_size=12,
        file_hash="a" * 64,
        process_status="uploaded",
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def fake_parse_result() -> SimpleNamespace:
    return SimpleNamespace(
        document_id=DOCUMENT_ID,
        process_status="parsed",
        chunk_count=2,
        parser_name="mineru_api",
        parser_version="test-v1",
    )


def fake_embedding_result() -> SimpleNamespace:
    return SimpleNamespace(
        document_id=DOCUMENT_ID,
        total=3,
        embedded=2,
        skipped=1,
        failed=0,
        model="Qwen3-Embedding-0.6B",
        dim=1024,
        device="cuda",
    )


def fake_embedding_status() -> SimpleNamespace:
    return SimpleNamespace(
        document_id=DOCUMENT_ID,
        total=3,
        not_started=0,
        embedding=0,
        embedded=2,
        embed_failed=1,
        models=["Qwen3-Embedding-0.6B"],
        dims=[1024],
    )


def fake_chunk() -> SimpleNamespace:
    # Historical persisted provenance remains readable after parser retirement.
    now = datetime(2026, 6, 30, 12, 30, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        chunk_index=0,
        content="铸型工艺 chunk",
        token_count=None,
        page_start=None,
        page_end=None,
        section_title=None,
        chunk_type="text",
        source_metadata={
            "character_count": 11,
            "parser_name": "simple",
            "parser_version": "0.1.0",
            "source_type": "text",
            "placeholder": False,
            "original_extension": ".txt",
        },
        embedding_status="not_started",
        created_at=now,
        updated_at=now,
    )


def test_upload_document_success(monkeypatch) -> None:
    def fake_create_document_from_upload(db, *, original_filename, content, content_type, storage_client=None):
        assert original_filename == "test.pdf"
        assert content == b"%PDF-1.4 test"
        assert content_type == "application/pdf"
        return fake_document()

    monkeypatch.setattr(documents_api, "create_document_from_upload", fake_create_document_from_upload)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("test.pdf", b"%PDF-1.4 test", "application/pdf")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["original_filename"] == "test.pdf"
    assert body["data"]["process_status"] == "uploaded"
    assert body["error"] is None


def test_upload_document_invalid_file_type(monkeypatch) -> None:
    def fake_create_document_from_upload(db, *, original_filename, content, content_type, storage_client=None):
        raise BusinessError(INVALID_FILE_TYPE, "不支持的文件类型。", status_code=400)

    monkeypatch.setattr(documents_api, "create_document_from_upload", fake_create_document_from_upload)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("test.exe", b"not allowed", "application/octet-stream")},
    )

    assert response.status_code == 415
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == INVALID_FILE_TYPE


def test_upload_document_file_too_large(monkeypatch) -> None:
    def fake_create_document_from_upload(db, *, original_filename, content, content_type, storage_client=None):
        raise BusinessError(FILE_TOO_LARGE, "上传文件超过大小限制。", status_code=413)

    monkeypatch.setattr(documents_api, "create_document_from_upload", fake_create_document_from_upload)

    response = client.post(
        "/api/v1/documents",
        files={"file": ("large.pdf", b"small test body", "application/pdf")},
    )

    assert response.status_code == 413
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == FILE_TOO_LARGE


def test_list_documents_success(monkeypatch) -> None:
    def fake_list_documents(db, *, limit, offset):
        assert limit == 20
        assert offset == 0
        return [fake_document()], 1

    monkeypatch.setattr(documents_api, "list_documents", fake_list_documents)

    response = client.get("/api/v1/documents?limit=20&offset=0")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["total"] == 1
    assert body["data"]["limit"] == 20
    assert body["data"]["offset"] == 0
    assert body["data"]["items"][0]["original_filename"] == "test.pdf"
    assert body["error"] is None


def test_get_document_success(monkeypatch) -> None:
    def fake_get_document_by_id(db, document_id):
        assert document_id == DOCUMENT_ID
        return fake_document()

    monkeypatch.setattr(documents_api, "get_document_by_id", fake_get_document_by_id)

    response = client.get(f"/api/v1/documents/{DOCUMENT_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["original_filename"] == "test.pdf"
    assert body["data"]["object_key"] == f"raw/2026/06/{DOCUMENT_ID}.pdf"
    assert body["data"]["process_status"] == "uploaded"
    assert "download_url" not in body["data"]
    assert "preview_url" not in body["data"]
    assert body["error"] is None


def test_get_document_not_found(monkeypatch) -> None:
    def fake_get_document_by_id(db, document_id):
        raise BusinessError(
            DOCUMENT_NOT_FOUND,
            "文档不存在。",
            detail={"document_id": str(document_id)},
            status_code=404,
        )

    monkeypatch.setattr(documents_api, "get_document_by_id", fake_get_document_by_id)

    response = client.get(f"/api/v1/documents/{DOCUMENT_ID}")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == DOCUMENT_NOT_FOUND


def test_parse_document_success(monkeypatch) -> None:
    def fake_parse_document(db, document_id):
        assert document_id == DOCUMENT_ID
        return fake_parse_result()

    monkeypatch.setattr(documents_api, "parse_document", fake_parse_document)

    response = client.post(f"/api/v1/documents/{DOCUMENT_ID}/parse")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["document_id"] == str(DOCUMENT_ID)
    assert body["data"]["process_status"] == "parsed"
    assert body["data"]["chunk_count"] == 2
    assert body["data"]["parser_name"] == "mineru_api"
    assert body["data"]["parser_version"] == "test-v1"
    assert body["error"] is None


def test_parse_document_already_parsed(monkeypatch) -> None:
    def fake_parse_document(db, document_id):
        raise BusinessError(DOCUMENT_ALREADY_PARSED, "文档已存在切片。", status_code=409)

    monkeypatch.setattr(documents_api, "parse_document", fake_parse_document)

    response = client.post(f"/api/v1/documents/{DOCUMENT_ID}/parse")

    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == DOCUMENT_ALREADY_PARSED


def test_parse_document_not_found(monkeypatch) -> None:
    def fake_parse_document(db, document_id):
        raise BusinessError(DOCUMENT_NOT_FOUND, "文档不存在。", status_code=404)

    monkeypatch.setattr(documents_api, "parse_document", fake_parse_document)

    response = client.post(f"/api/v1/documents/{DOCUMENT_ID}/parse")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == DOCUMENT_NOT_FOUND


def test_parse_document_parser_unavailable(monkeypatch) -> None:
    def fake_parse_document(db, document_id):
        raise BusinessError(DOCUMENT_PARSER_UNAVAILABLE, "解析器不可用。", status_code=503)

    monkeypatch.setattr(documents_api, "parse_document", fake_parse_document)

    response = client.post(f"/api/v1/documents/{DOCUMENT_ID}/parse")

    assert response.status_code == 503
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == DOCUMENT_PARSER_UNAVAILABLE


def test_parse_document_chunk_config_invalid(monkeypatch) -> None:
    def fake_parse_document(db, document_id):
        raise BusinessError(DOCUMENT_CHUNK_CONFIG_INVALID, "切块参数非法。", status_code=400)

    monkeypatch.setattr(documents_api, "parse_document", fake_parse_document)

    response = client.post(f"/api/v1/documents/{DOCUMENT_ID}/parse")

    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == DOCUMENT_CHUNK_CONFIG_INVALID


def test_get_document_chunks_success(monkeypatch) -> None:
    def fake_list_document_chunks(db, document_id, *, limit, offset):
        assert document_id == DOCUMENT_ID
        assert limit == 50
        assert offset == 0
        return SimpleNamespace(
            items=[fake_chunk()],
            total=1,
            limit=limit,
            offset=offset,
            stats={
                "chunk_count": 1,
                "total_characters": 11,
                "min_characters": 11,
                "max_characters": 11,
                "avg_characters": 11.0,
            },
        )

    monkeypatch.setattr(documents_api, "list_document_chunks", fake_list_document_chunks)

    response = client.get(f"/api/v1/documents/{DOCUMENT_ID}/chunks")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["total"] == 1
    assert body["data"]["limit"] == 50
    assert body["data"]["offset"] == 0
    assert body["data"]["items"][0]["id"] == str(CHUNK_ID)
    assert body["data"]["items"][0]["chunk_index"] == 0
    assert body["data"]["items"][0]["character_count"] == 11
    assert body["data"]["items"][0]["embedding_status"] == "not_started"
    assert body["data"]["stats"]["chunk_count"] == 1
    assert body["error"] is None


def test_get_document_chunks_rejects_invalid_limit_and_offset() -> None:
    for query in ("limit=0", "limit=101", "offset=-1"):
        response = client.get(f"/api/v1/documents/{DOCUMENT_ID}/chunks?{query}")

        assert response.status_code == 422


def test_get_document_chunks_not_found(monkeypatch) -> None:
    def fake_list_document_chunks(db, document_id, *, limit, offset):
        raise BusinessError(DOCUMENT_NOT_FOUND, "文档不存在。", status_code=404)

    monkeypatch.setattr(documents_api, "list_document_chunks", fake_list_document_chunks)

    response = client.get(f"/api/v1/documents/{DOCUMENT_ID}/chunks")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == DOCUMENT_NOT_FOUND


def test_generate_document_embeddings_success(monkeypatch) -> None:
    def fake_generate_document_embeddings(db, document_id):
        assert document_id == DOCUMENT_ID
        return fake_embedding_result()

    monkeypatch.setattr(documents_api, "generate_document_embeddings", fake_generate_document_embeddings)

    response = client.post(f"/api/v1/documents/{DOCUMENT_ID}/embeddings")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["document_id"] == str(DOCUMENT_ID)
    assert body["data"]["total"] == 3
    assert body["data"]["embedded"] == 2
    assert body["data"]["skipped"] == 1
    assert body["data"]["failed"] == 0
    assert body["data"]["model"] == "Qwen3-Embedding-0.6B"
    assert body["data"]["dim"] == 1024
    assert body["data"]["device"] == "cuda"
    assert body["error"] is None


def test_generate_document_embeddings_not_found(monkeypatch) -> None:
    def fake_generate_document_embeddings(db, document_id):
        raise BusinessError(DOCUMENT_NOT_FOUND, "document not found", status_code=404)

    monkeypatch.setattr(documents_api, "generate_document_embeddings", fake_generate_document_embeddings)

    response = client.post(f"/api/v1/documents/{DOCUMENT_ID}/embeddings")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == DOCUMENT_NOT_FOUND


def test_generate_document_embeddings_document_not_parsed(monkeypatch) -> None:
    def fake_generate_document_embeddings(db, document_id):
        raise BusinessError(DOCUMENT_NOT_PARSED, "document not parsed", status_code=409)

    monkeypatch.setattr(documents_api, "generate_document_embeddings", fake_generate_document_embeddings)

    response = client.post(f"/api/v1/documents/{DOCUMENT_ID}/embeddings")

    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == DOCUMENT_NOT_PARSED


def test_generate_document_embeddings_already_generated(monkeypatch) -> None:
    def fake_generate_document_embeddings(db, document_id):
        raise BusinessError(
            DOCUMENT_EMBEDDINGS_ALREADY_GENERATED,
            "embeddings already generated",
            status_code=409,
        )

    monkeypatch.setattr(documents_api, "generate_document_embeddings", fake_generate_document_embeddings)

    response = client.post(f"/api/v1/documents/{DOCUMENT_ID}/embeddings")

    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == DOCUMENT_EMBEDDINGS_ALREADY_GENERATED


def test_generate_document_embeddings_generation_failed(monkeypatch) -> None:
    def fake_generate_document_embeddings(db, document_id):
        raise BusinessError(EMBEDDING_GENERATION_FAILED, "embedding generation failed", status_code=500)

    monkeypatch.setattr(documents_api, "generate_document_embeddings", fake_generate_document_embeddings)

    response = client.post(f"/api/v1/documents/{DOCUMENT_ID}/embeddings")

    assert response.status_code == 500
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == EMBEDDING_GENERATION_FAILED


def test_get_document_embedding_status_success(monkeypatch) -> None:
    def fake_get_document_embedding_status(db, document_id):
        assert document_id == DOCUMENT_ID
        return fake_embedding_status()

    monkeypatch.setattr(documents_api, "get_document_embedding_status", fake_get_document_embedding_status)

    response = client.get(f"/api/v1/documents/{DOCUMENT_ID}/embedding-status")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["document_id"] == str(DOCUMENT_ID)
    assert body["data"]["total"] == 3
    assert body["data"]["not_started"] == 0
    assert body["data"]["embedding"] == 0
    assert body["data"]["embedded"] == 2
    assert body["data"]["embed_failed"] == 1
    assert body["data"]["models"] == ["Qwen3-Embedding-0.6B"]
    assert body["data"]["dims"] == [1024]
    assert body["error"] is None


def test_get_document_embedding_status_not_found(monkeypatch) -> None:
    def fake_get_document_embedding_status(db, document_id):
        raise BusinessError(DOCUMENT_NOT_FOUND, "document not found", status_code=404)

    monkeypatch.setattr(documents_api, "get_document_embedding_status", fake_get_document_embedding_status)

    response = client.get(f"/api/v1/documents/{DOCUMENT_ID}/embedding-status")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == DOCUMENT_NOT_FOUND
