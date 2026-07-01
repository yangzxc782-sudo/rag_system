from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from app.api.v1 import documents as documents_api
from app.core.errors import DOCUMENT_NOT_FOUND, FILE_TOO_LARGE, INVALID_FILE_TYPE, BusinessError
from app.main import app


client = TestClient(app)


DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


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
