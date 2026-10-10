from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import documents as api
from app.core.errors import BusinessError
from app.ingestion.block_chunker import BlockChunkerConfig, SEGMENTATION_VERSION
from app.db.session import get_db
from test_document_processing import db, settings, setup


def client_for(db, settings):
    application = FastAPI()
    application.include_router(api.router, prefix="/api/v1")
    application.state.settings = settings
    application.state.document_processing_executor = SimpleNamespace(wake=Mock())
    application.dependency_overrides[get_db] = lambda: db
    return TestClient(application), application


def test_process_post_returns_202_then_get_is_read_only_and_cancel_is_explicit(db, settings, monkeypatch):
    doc, state, *_ = setup(db, settings, monkeypatch)
    client, application = client_for(db, settings)
    with client:
        # Services use SQLite in one thread in other tests; API calls here use
        # stable service snapshots so no thread-affinity bypass is necessary.
        monkeypatch.setattr(api, "request_processing", Mock(return_value=state))
        monkeypatch.setattr(api, "list_processing_jobs", Mock(return_value=dict(items=[state], total=1,
            can_process=False, executor_enabled=True, search_enabled=False,
            segmentation_defaults=BlockChunkerConfig().model_dump(), segmentation_version=SEGMENTATION_VERSION)))
        monkeypatch.setattr(api, "processing_status", Mock(return_value=state))
        monkeypatch.setattr(api, "cancel_processing", Mock(return_value={**state, "status": "cancelled", "can_cancel": False}))
        response = client.post(f"/api/v1/documents/{doc}/process", json={"request_id": str(state["request_id"]),
            "config": {"max_chunk_chars": 50, "min_chunk_chars": 0, "overlap_chars": 5}})
        assert response.status_code == 202
        assert response.json()["data"]["job_id"] == str(state["job_id"])
        application.state.document_processing_executor.wake.assert_called_once()
        assert client.get(f"/api/v1/documents/{doc}/processing-jobs").status_code == 200
        assert client.get(f"/api/v1/documents/{doc}/processing-jobs/{state['job_id']}").status_code == 200
        application.state.document_processing_executor.wake.assert_called_once()
        assert client.post(f"/api/v1/documents/{doc}/processing-jobs/{state['job_id']}/cancel").status_code == 200
        assert "checkpoint" not in response.json()["data"]


def test_api_disabled_conflict_and_invalid_config_do_not_wake(db, settings, monkeypatch):
    client, application = client_for(db, settings)
    start = Mock(side_effect=BusinessError("DOCUMENT_PROCESSING_EXECUTOR_DISABLED", "disabled", status_code=503))
    monkeypatch.setattr(api, "request_processing", start)
    with client:
        path = f"/api/v1/documents/{uuid4()}/process"
        assert client.post(path, json={"request_id": str(uuid4())}).status_code == 503
        assert client.post(path, json={"request_id": str(uuid4()), "config": {"chunk_size": 10, "overlap": 10}}).status_code == 422
        assert client.post(path, json={"request_id": str(uuid4()), "source_spans": []}).status_code == 422
        assert start.call_count == 1
        application.state.document_processing_executor.wake.assert_not_called()


def test_resume_omission_and_explicit_default_are_distinct(db, settings, monkeypatch):
    doc, state, *_ = setup(db, settings, monkeypatch)
    client, application = client_for(db, settings)
    resume = Mock(return_value=state)
    monkeypatch.setattr(api, "manage_rechunk", resume)
    with client:
        path = f"/api/v1/documents/{doc}/processing-jobs/{state['job_id']}/resume"
        assert client.post(path, json={}).status_code == 202
        assert resume.call_args.kwargs["config"] is None
        assert client.post(path, json={"config": {}}).status_code == 202
        assert resume.call_args.kwargs["config"] == BlockChunkerConfig()
        assert client.post(path, json={"config": None}).status_code == 422
        assert resume.call_count == 2


@pytest.mark.parametrize("body", [{}, {"config": {}}, {"config": BlockChunkerConfig().model_dump()}])
def test_process_defaults_reach_service_as_one_complete_config(db, settings, monkeypatch, body):
    doc, state, *_ = setup(db, settings, monkeypatch)
    client, _ = client_for(db, settings)
    start = Mock(return_value=state)
    monkeypatch.setattr(api, "request_processing", start)
    with client:
        response = client.post(f"/api/v1/documents/{doc}/process", json={"request_id": str(uuid4()), **body})
        assert response.status_code == 202
        effective = start.call_args.args[3]
        assert effective.model_dump() == BlockChunkerConfig().model_dump()
        assert effective.fingerprint == BlockChunkerConfig().fingerprint


@pytest.mark.parametrize("config", [None, {"boundary": "line"}, {"chunk_size": 1000}, {"overlap": 100}])
def test_process_and_resume_reject_old_or_null_config_before_services(db, settings, monkeypatch, config):
    client, application = client_for(db, settings)
    start, resume = Mock(), Mock()
    monkeypatch.setattr(api, "request_processing", start)
    monkeypatch.setattr(api, "manage_rechunk", resume)
    with client:
        base = f"/api/v1/documents/{uuid4()}"
        assert client.post(f"{base}/process", json={"request_id": str(uuid4()), "config": config}).status_code == 422
        assert client.post(f"{base}/processing-jobs/{uuid4()}/resume", json={"config": config}).status_code == 422
        start.assert_not_called()
        resume.assert_not_called()
        application.state.document_processing_executor.wake.assert_not_called()
