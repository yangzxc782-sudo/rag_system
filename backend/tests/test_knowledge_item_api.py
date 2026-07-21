from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import knowledge_items as knowledge_items_api
from app.api.v1.router import api_router
from app.core.errors import (
    KNOWLEDGE_ITEM_DUPLICATE,
    KNOWLEDGE_ITEM_CONFIG_INVALID,
    KNOWLEDGE_ITEM_EXTRACTION_FAILED,
    KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED,
    KNOWLEDGE_ITEM_INVALID_TRANSITION,
    KNOWLEDGE_ITEM_NOT_FOUND,
    KNOWLEDGE_ITEM_REVIEW_FAILED,
    KNOWLEDGE_ITEM_VALIDATION_FAILED,
    KNOWLEDGE_ITEM_VERSION_FAILED,
    LLM_AUTHENTICATION_FAILED,
    LLM_CONFIG_INVALID,
    LLM_EMPTY_CONTENT,
    LLM_GENERATION_FAILED,
    LLM_JSON_INVALID,
    LLM_MODEL_NOT_FOUND,
    LLM_PARAMETER_UNSUPPORTED,
    LLM_PERMISSION_DENIED,
    LLM_PROVIDER_INVALID,
    LLM_RATE_LIMITED,
    LLM_REQUEST_INVALID,
    LLM_REQUEST_REJECTED,
    LLM_RESPONSE_INVALID,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    LLM_UPSTREAM_FAILED,
    BusinessError,
)


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[knowledge_items_api.get_db] = lambda: object()
    return TestClient(app)


def make_item(status: str = "draft") -> SimpleNamespace:
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    item_id = uuid4()
    chunk_id = uuid4()
    return SimpleNamespace(
        id=item_id,
        item_type="process_rule",
        title="Riser rule",
        content="Place risers near hot spots.",
        content_hash="a" * 64,
        structured_data={"kind": "rule"},
        entities=["riser"],
        parameters=None,
        conditions=None,
        confidence=0.8,
        status=status,
        source_document_id=uuid4(),
        source_filename="casting.md",
        created_by="system",
        reviewed_by=None,
        review_comment=None,
        version=1,
        reviewed_at=None,
        created_at=now,
        updated_at=now,
        revises_item_id=None,
        chunks=[SimpleNamespace(chunk_id=chunk_id)],
    )


def make_chunk(item: SimpleNamespace) -> SimpleNamespace:
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        knowledge_item_id=item.id,
        chunk_id=item.chunks[0].chunk_id,
        document_id=item.source_document_id,
        chunk_index=1,
        source_text="Source text snapshot.",
        created_at=now,
    )


def make_version(item: SimpleNamespace) -> SimpleNamespace:
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        knowledge_item_id=item.id,
        version=1,
        snapshot={"title": item.title, "source_chunk_ids": [str(item.chunks[0].chunk_id)]},
        change_reason="created",
        created_by="system",
        created_at=now,
    )


def make_review(item: SimpleNamespace) -> SimpleNamespace:
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        knowledge_item_id=item.id,
        review_action="approve",
        from_status="pending_review",
        to_status="approved",
        review_comment="ok",
        reviewer="expert",
        created_at=now,
    )


def test_router_registers_knowledge_items_without_removing_search_or_rag() -> None:
    paths = set(make_client().app.openapi().get("paths", {}).keys())

    assert "/api/v1/knowledge-items" in paths
    assert "/api/v1/knowledge-items/extract" in paths
    assert "/api/v1/knowledge-items/{item_id}" in paths
    assert "/api/v1/knowledge-items/{item_id}/chunks" in paths
    assert "/api/v1/knowledge-items/{item_id}/versions" in paths
    assert "/api/v1/knowledge-items/{item_id}/submit" in paths
    assert "/api/v1/knowledge-items/{item_id}/approve" in paths
    assert "/api/v1/knowledge-items/{item_id}/reject" in paths
    assert "/api/v1/knowledge-items/{item_id}/deprecate" in paths
    assert "/api/v1/knowledge-items/{item_id}/reviews" in paths
    assert "/api/v1/knowledge-items/{item_id}/revise" in paths
    assert "/api/v1/search" in paths
    assert "/api/v1/search/vector" in paths
    assert "/api/v1/rag/ask" in paths


def test_extraction_openapi_request_schema_does_not_add_messages_or_history() -> None:
    schemas = make_client().app.openapi()["components"]["schemas"]
    schema = schemas["KnowledgeExtractionRequest"]

    assert set(schema["properties"]) == {
        "mode",
        "document_id",
        "chunk_ids",
        "item_types",
        "auto_submit",
        "max_chunks",
        "created_by",
    }
    assert "messages" not in schema["properties"]
    assert "history" not in schema["properties"]
    assert set(schemas["KnowledgeExtractionData"]["properties"]) == {
        "items",
        "created",
        "skipped_duplicates",
        "status",
        "auto_submit",
        "llm",
    }


def test_list_knowledge_items_success(monkeypatch: pytest.MonkeyPatch) -> None:
    item = make_item()

    def fake_list_knowledge_items(db: object, **kwargs: object) -> SimpleNamespace:
        assert kwargs["limit"] == 20
        assert kwargs["offset"] == 0
        return SimpleNamespace(items=[item], total=1, limit=20, offset=0)

    monkeypatch.setattr(knowledge_items_api.knowledge_item_service, "list_knowledge_items", fake_list_knowledge_items)

    response = make_client().get("/api/v1/knowledge-items")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["confidence"] == 0.8


def test_get_create_patch_chunks_and_versions_success(monkeypatch: pytest.MonkeyPatch) -> None:
    item = make_item()
    chunk = make_chunk(item)
    version = make_version(item)

    monkeypatch.setattr(knowledge_items_api.knowledge_item_service, "get_knowledge_item", lambda db, item_id: item)
    monkeypatch.setattr(knowledge_items_api.knowledge_item_service, "create_knowledge_item", lambda db, request: item)
    monkeypatch.setattr(
        knowledge_items_api.knowledge_item_service,
        "update_knowledge_item",
        lambda db, item_id, request: item,
    )
    monkeypatch.setattr(
        knowledge_items_api.knowledge_item_service,
        "get_knowledge_item_chunks",
        lambda db, item_id: [chunk],
    )
    monkeypatch.setattr(
        knowledge_items_api.knowledge_item_service,
        "get_knowledge_item_versions",
        lambda db, item_id: [version],
    )

    client = make_client()
    assert client.get(f"/api/v1/knowledge-items/{item.id}").status_code == 200
    assert client.post(
        "/api/v1/knowledge-items",
        json={"item_type": "process_rule", "title": "Riser rule", "content": "Content"},
    ).status_code == 201
    assert client.patch(f"/api/v1/knowledge-items/{item.id}", json={"title": "Updated"}).status_code == 200
    assert client.get(f"/api/v1/knowledge-items/{item.id}/chunks").json()["data"]["items"][0]["source_text"] == (
        "Source text snapshot."
    )
    assert client.get(f"/api/v1/knowledge-items/{item.id}/versions").json()["data"]["items"][0]["version"] == 1


def test_review_and_revise_endpoints_success(monkeypatch: pytest.MonkeyPatch) -> None:
    item = make_item("pending_review")
    approved_item = make_item("approved")
    rejected_item = make_item("rejected")
    deprecated_item = make_item("deprecated")
    revision = make_item("draft")
    revision.revises_item_id = approved_item.id
    review = make_review(item)

    monkeypatch.setattr(
        knowledge_items_api.knowledge_item_service,
        "submit_knowledge_item",
        lambda db, item_id, **kwargs: item,
    )
    monkeypatch.setattr(
        knowledge_items_api.knowledge_item_service,
        "approve_knowledge_item",
        lambda db, item_id, **kwargs: approved_item,
    )
    monkeypatch.setattr(
        knowledge_items_api.knowledge_item_service,
        "reject_knowledge_item",
        lambda db, item_id, **kwargs: rejected_item,
    )
    monkeypatch.setattr(
        knowledge_items_api.knowledge_item_service,
        "deprecate_knowledge_item",
        lambda db, item_id, **kwargs: deprecated_item,
    )
    monkeypatch.setattr(
        knowledge_items_api.knowledge_item_service,
        "list_knowledge_item_reviews",
        lambda db, item_id: [review],
    )
    monkeypatch.setattr(
        knowledge_items_api.knowledge_item_service,
        "revise_knowledge_item",
        lambda db, item_id, **kwargs: revision,
    )

    client = make_client()
    assert client.post(f"/api/v1/knowledge-items/{item.id}/submit", json={"reviewer": "expert"}).status_code == 200
    assert client.post(f"/api/v1/knowledge-items/{item.id}/approve", json={"review_comment": "ok"}).status_code == 200
    reject_response = client.post(f"/api/v1/knowledge-items/{item.id}/reject", json={"reviewer": "expert"})
    assert reject_response.json()["data"]["status"] == "rejected"
    assert client.post(f"/api/v1/knowledge-items/{item.id}/deprecate", json={}).json()["data"]["status"] == "deprecated"
    assert client.get(f"/api/v1/knowledge-items/{item.id}/reviews").json()["data"]["items"][0]["review_action"] == (
        "approve"
    )
    revise_body = client.post(f"/api/v1/knowledge-items/{item.id}/revise", json={"created_by": "editor"}).json()
    assert revise_body["data"]["status"] == "draft"
    assert revise_body["data"]["revises_item_id"] == str(approved_item.id)


def test_extract_knowledge_items_success(monkeypatch: pytest.MonkeyPatch) -> None:
    item = make_item("draft")
    skipped = SimpleNamespace(
        item_type="process_rule",
        title="Duplicate rule",
        content_hash="b" * 64,
        source_document_id=item.source_document_id,
        reason="duplicate",
    )
    result = SimpleNamespace(
        items=[item],
        created=1,
        skipped_duplicates=[skipped],
        status="draft",
        auto_submit=False,
        llm_provider="fake",
        llm_model="fake-model",
    )

    monkeypatch.setattr(
        knowledge_items_api.knowledge_extraction_service,
        "extract_knowledge_items",
        lambda db, request: result,
    )

    response = make_client().post(
        "/api/v1/knowledge-items/extract",
        json={"mode": "chunks", "chunk_ids": [str(item.chunks[0].chunk_id)]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["created"] == 1
    assert body["data"]["items"][0]["status"] == "draft"
    assert body["data"]["skipped_duplicates"][0]["reason"] == "duplicate"
    assert body["data"]["llm"]["model"] == "fake-model"


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        (KNOWLEDGE_ITEM_CONFIG_INVALID, 400),
        (KNOWLEDGE_ITEM_EXTRACTION_FAILED, 500),
        (KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED, 500),
        (KNOWLEDGE_ITEM_NOT_FOUND, 404),
        (KNOWLEDGE_ITEM_DUPLICATE, 409),
        (KNOWLEDGE_ITEM_INVALID_TRANSITION, 409),
        (KNOWLEDGE_ITEM_VALIDATION_FAILED, 400),
        (KNOWLEDGE_ITEM_REVIEW_FAILED, 500),
        (KNOWLEDGE_ITEM_VERSION_FAILED, 500),
        (LLM_CONFIG_INVALID, 400),
        (LLM_PROVIDER_INVALID, 400),
        (LLM_REQUEST_INVALID, 400),
        (LLM_PARAMETER_UNSUPPORTED, 400),
        (LLM_AUTHENTICATION_FAILED, 502),
        (LLM_PERMISSION_DENIED, 502),
        (LLM_MODEL_NOT_FOUND, 502),
        (LLM_RATE_LIMITED, 429),
        (LLM_REQUEST_REJECTED, 502),
        (LLM_UPSTREAM_FAILED, 502),
        (LLM_RESPONSE_INVALID, 502),
        (LLM_EMPTY_CONTENT, 502),
        (LLM_JSON_INVALID, 502),
        (LLM_UNAVAILABLE, 503),
        (LLM_TIMEOUT, 504),
        (LLM_GENERATION_FAILED, 500),
    ],
)
def test_knowledge_item_api_maps_business_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    expected_status: int,
) -> None:
    def raise_error(*_args: object, **_kwargs: object) -> object:
        raise BusinessError(error_code, "failed", status_code=500)

    monkeypatch.setattr(knowledge_items_api.knowledge_item_service, "get_knowledge_item", raise_error)

    response = make_client().get(f"/api/v1/knowledge-items/{uuid4()}")

    assert response.status_code == expected_status
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == error_code


def test_create_approved_status_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_create(*_args: object, **_kwargs: object) -> object:
        raise BusinessError(KNOWLEDGE_ITEM_VALIDATION_FAILED, "status cannot be approved", status_code=400)

    monkeypatch.setattr(knowledge_items_api.knowledge_item_service, "create_knowledge_item", fake_create)

    response = make_client().post(
        "/api/v1/knowledge-items",
        json={"item_type": "process_rule", "title": "Riser rule", "content": "Content", "status": "approved"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == KNOWLEDGE_ITEM_VALIDATION_FAILED


def test_invalid_limit_returns_request_validation_error() -> None:
    response = make_client().get("/api/v1/knowledge-items?limit=0")

    assert response.status_code == 422
