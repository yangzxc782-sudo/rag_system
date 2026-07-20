from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import documents as documents_api
from app.core.errors import (
    DOCUMENT_NOT_FOUND,
    DOCUMENT_PARSE_RUN_NOT_FOUND,
    BusinessError,
)
from app.main import app
from app.models.document import Document
from app.models.document_parse_run import DocumentParseRun
from app.services.document_assets import list_assets as list_assets_service
from app.services.document_blocks import list_blocks as list_blocks_service

client = TestClient(app)

DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
OTHER_DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440099")
PARSE_RUN_ID = UUID("660e8400-e29b-41d4-a716-446655440001")
BLOCK_ID = UUID("770e8400-e29b-41d4-a716-446655440002")
ASSET_ID = UUID("880e8400-e29b-41d4-a716-446655440003")
NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


def fake_parse_run(**overrides):
    values = {
        "id": PARSE_RUN_ID,
        "document_id": DOCUMENT_ID,
        "parser_provider": "mineru_api",
        "parser_version": "test-v1",
        "parse_mode": "auto",
        "status": "succeeded",
        "is_active": True,
        "input_file_key": f"raw/2026/07/{DOCUMENT_ID}.pdf",
        "output_prefix": f"parsed-assets/{DOCUMENT_ID}/{PARSE_RUN_ID}",
        "output_markdown_key": (
            f"parsed-assets/{DOCUMENT_ID}/{PARSE_RUN_ID}/output.md"
        ),
        "output_json_key": (
            f"parsed-assets/{DOCUMENT_ID}/{PARSE_RUN_ID}/output.json"
        ),
        "output_markdown_status": "saved",
        "output_json_status": "download_deferred",
        "failure_status_persisted": None,
        "page_count": 3,
        "block_count": 4,
        "asset_count": 2,
        "error_message": None,
        "source_metadata_summary": {
            "task_id": "safe-task-id",
            "deferred_file_count": 1,
        },
        "started_at": NOW,
        "completed_at": NOW,
        "created_at": NOW,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def fake_block():
    return SimpleNamespace(
        id=BLOCK_ID,
        document_id=DOCUMENT_ID,
        parse_run_id=PARSE_RUN_ID,
        block_index=0,
        block_key="title-1",
        block_type="title",
        page_start=1,
        page_end=1,
        bbox=[10, 20, 300, 60],
        text="Casting",
        markdown=None,
        html=None,
        latex=None,
        caption=None,
        parent_block_key=None,
        section_path=["Casting"],
        confidence=0.98,
        source_metadata_summary={
            "original_type": "title",
            "mineru_block_id": "title-1",
        },
        content_truncated=False,
        created_at=NOW,
        source_metadata={
            "full_mineru_json": {"must_not": "be returned"},
        },
    )


def fake_asset():
    return SimpleNamespace(
        id=ASSET_ID,
        document_id=DOCUMENT_ID,
        parse_run_id=PARSE_RUN_ID,
        asset_type="image",
        page_number=1,
        asset_key=(
            f"parsed-assets/{DOCUMENT_ID}/{PARSE_RUN_ID}/images/mould.png"
        ),
        filename="mould.png",
        mime_type="image/png",
        size_bytes=128,
        caption="Mould layout",
        source_block_key="image-1",
        source_metadata_summary={"width": 800, "height": 600},
        created_at=NOW,
        content=b"must-not-be-returned",
    )


class CrossDocumentParseRunSession:
    def get(self, model, entity_id):
        if model is Document and entity_id == DOCUMENT_ID:
            return SimpleNamespace(id=DOCUMENT_ID)
        if model is DocumentParseRun and entity_id == PARSE_RUN_ID:
            return SimpleNamespace(
                id=PARSE_RUN_ID,
                document_id=OTHER_DOCUMENT_ID,
            )
        return None

    def scalar(self, *args, **kwargs):
        raise AssertionError(
            "query should not run after parse_run ownership fails"
        )

    def scalars(self, *args, **kwargs):
        raise AssertionError(
            "query should not run after parse_run ownership fails"
        )


def test_parse_runs_api_returns_paginated_list(monkeypatch) -> None:
    captured = {}

    def fake_list(db, document_id, *, limit, offset):
        captured.update(
            document_id=document_id,
            limit=limit,
            offset=offset,
        )
        return SimpleNamespace(
            items=[fake_parse_run()],
            total=1,
            limit=limit,
            offset=offset,
        )

    monkeypatch.setattr(
        documents_api,
        "list_document_parse_runs",
        fake_list,
    )

    response = client.get(
        f"/api/v1/documents/{DOCUMENT_ID}/parse-runs?limit=25&offset=0"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["status"] == "succeeded"
    assert captured == {
        "document_id": DOCUMENT_ID,
        "limit": 25,
        "offset": 0,
    }


def test_parse_status_api_returns_latest_active_and_output_states(
    monkeypatch,
) -> None:
    parse_run = fake_parse_run(failure_status_persisted=False)
    monkeypatch.setattr(
        documents_api,
        "get_document_parse_status",
        lambda db, document_id: SimpleNamespace(
            document_id=document_id,
            process_status="parsed",
            latest_parse_run=parse_run,
            active_parse_run=parse_run,
        ),
    )

    response = client.get(
        f"/api/v1/documents/{DOCUMENT_ID}/parse-status"
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["process_status"] == "parsed"
    assert data["latest_parse_run"]["output_markdown_status"] == "saved"
    assert (
        data["active_parse_run"]["output_json_status"]
        == "download_deferred"
    )
    assert (
        data["latest_parse_run"]["failure_status_persisted"] is False
    )


def test_blocks_api_forwards_filters_and_pagination(monkeypatch) -> None:
    captured = {}

    def fake_list(
        db,
        document_id,
        *,
        parse_run_id,
        block_type,
        limit,
        offset,
    ):
        captured.update(
            document_id=document_id,
            parse_run_id=parse_run_id,
            block_type=block_type,
            limit=limit,
            offset=offset,
        )
        return SimpleNamespace(
            items=[fake_block()],
            total=1,
            limit=limit,
            offset=offset,
        )

    monkeypatch.setattr(documents_api, "list_document_blocks", fake_list)

    response = client.get(
        f"/api/v1/documents/{DOCUMENT_ID}/blocks"
        f"?parse_run_id={PARSE_RUN_ID}&block_type=title&limit=20&offset=5"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["items"][0]["block_type"] == "title"
    assert "source_metadata" not in body["data"]["items"][0]
    assert "full_mineru_json" not in response.text
    assert captured == {
        "document_id": DOCUMENT_ID,
        "parse_run_id": PARSE_RUN_ID,
        "block_type": "title",
        "limit": 20,
        "offset": 5,
    }


def test_assets_api_forwards_filters_without_binary_content(
    monkeypatch,
) -> None:
    captured = {}

    def fake_list(
        db,
        document_id,
        *,
        parse_run_id,
        asset_type,
        limit,
        offset,
    ):
        captured.update(
            document_id=document_id,
            parse_run_id=parse_run_id,
            asset_type=asset_type,
            limit=limit,
            offset=offset,
        )
        return SimpleNamespace(
            items=[fake_asset()],
            total=1,
            limit=limit,
            offset=offset,
        )

    monkeypatch.setattr(documents_api, "list_document_assets", fake_list)

    response = client.get(
        f"/api/v1/documents/{DOCUMENT_ID}/assets"
        f"?parse_run_id={PARSE_RUN_ID}&asset_type=image&limit=10&offset=0"
    )

    assert response.status_code == 200
    item = response.json()["data"]["items"][0]
    body = response.json()["data"]
    assert body["total"] == 1
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert item["asset_key"].endswith("/images/mould.png")
    assert "content" not in item
    assert "must-not-be-returned" not in response.text
    assert captured == {
        "document_id": DOCUMENT_ID,
        "parse_run_id": PARSE_RUN_ID,
        "asset_type": "image",
        "limit": 10,
        "offset": 0,
    }


def test_blocks_and_assets_limit_is_capped_by_validation() -> None:
    blocks_response = client.get(
        f"/api/v1/documents/{DOCUMENT_ID}/blocks?limit=201"
    )
    assets_response = client.get(
        f"/api/v1/documents/{DOCUMENT_ID}/assets?limit=201"
    )

    assert blocks_response.status_code == 422
    assert assets_response.status_code == 422


def test_parse_result_apis_return_document_not_found(monkeypatch) -> None:
    def not_found(*args, **kwargs):
        raise BusinessError(
            DOCUMENT_NOT_FOUND,
            "文档不存在。",
            status_code=404,
        )

    monkeypatch.setattr(
        documents_api,
        "get_document_parse_status",
        not_found,
    )

    response = client.get(
        f"/api/v1/documents/{DOCUMENT_ID}/parse-status"
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == DOCUMENT_NOT_FOUND


def test_blocks_api_rejects_parse_run_from_another_document(
    monkeypatch,
) -> None:
    def wrong_parse_run(*args, **kwargs):
        raise BusinessError(
            DOCUMENT_PARSE_RUN_NOT_FOUND,
            "解析任务不属于当前文档。",
            status_code=404,
        )

    monkeypatch.setattr(
        documents_api,
        "list_document_blocks",
        wrong_parse_run,
    )

    response = client.get(
        f"/api/v1/documents/{DOCUMENT_ID}/blocks"
        f"?parse_run_id={PARSE_RUN_ID}"
    )

    assert response.status_code == 404
    assert (
        response.json()["error"]["code"]
        == DOCUMENT_PARSE_RUN_NOT_FOUND
    )


def test_blocks_service_rejects_cross_document_parse_run() -> None:
    with pytest.raises(BusinessError) as exc_info:
        list_blocks_service(
            CrossDocumentParseRunSession(),
            DOCUMENT_ID,
            parse_run_id=PARSE_RUN_ID,
        )

    assert exc_info.value.code == DOCUMENT_PARSE_RUN_NOT_FOUND
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == {
        "document_id": str(DOCUMENT_ID),
        "parse_run_id": str(PARSE_RUN_ID),
    }


def test_assets_service_rejects_cross_document_parse_run() -> None:
    with pytest.raises(BusinessError) as exc_info:
        list_assets_service(
            CrossDocumentParseRunSession(),
            DOCUMENT_ID,
            parse_run_id=PARSE_RUN_ID,
        )

    assert exc_info.value.code == DOCUMENT_PARSE_RUN_NOT_FOUND
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == {
        "document_id": str(DOCUMENT_ID),
        "parse_run_id": str(PARSE_RUN_ID),
    }
