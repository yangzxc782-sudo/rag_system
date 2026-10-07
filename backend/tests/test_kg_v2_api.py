"""No backend startup, real service calls or business database connections."""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.v1 import documents as api
from app.core.errors import BusinessError
from app.db.session import get_db


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: object()
    return TestClient(app)


def result(doc, build, source):
    return dict(graph_build_id=build, document_id=doc, source_version=source, graph_id="KG-20261006-test",
        status="ready", job_status="queued", stage="kg_ready", unit_counts={"succeeded_nonempty": 1},
        anchor_count=1, entity_count=2, relationship_count=1, error_code=None, attempt_count=2,
        max_attempts=5, lease_expires_at=None, can_advance=False, next_stage="chunking")


def test_create_status_and_explicit_step_never_chain_to_m3(client, monkeypatch):
    doc, build, source, request = uuid4(), uuid4(), uuid4(), uuid4()
    calls = []
    def prepare(db, did, sid, rid):
        calls.append((did, sid, rid))
        return result(did, build, sid)
    def advance(db, did, bid, retry):
        calls.append((did, bid, retry))
        return result(did, bid, source)
    monkeypatch.setattr(api, "prepare_graph_build", prepare)
    monkeypatch.setattr(api, "advance_graph_build", advance)
    monkeypatch.setattr(api, "graph_build_status", lambda db, did, bid: result(did, bid, source))
    path = f"/api/v1/documents/{doc}/graph-builds"
    response = client.post(path, json=dict(source_version=str(source), request_id=str(request)))
    assert response.status_code == 200 and calls == [(doc, source, request)]
    assert response.json()["data"]["can_advance"] is False
    assert client.get(f"{path}/{build}").status_code == 200
    assert client.post(f"{path}/{build}/advance", json={"retry": True}).status_code == 200
    assert calls[-1] == (doc, build, True)
    assert len(calls) == 2


def test_disabled_api_returns_sanitized_error_without_execution(client, monkeypatch):
    def disabled(*args):
        raise BusinessError("KG_BUILD_DISABLED", "构图尚未启用。", status_code=503)
    monkeypatch.setattr(api, "prepare_graph_build", disabled)
    response = client.post(f"/api/v1/documents/{uuid4()}/graph-builds",
                           json=dict(source_version=str(uuid4()), request_id=str(uuid4())))
    assert response.status_code == 503 and response.json()["error"]["code"] == "KG_BUILD_DISABLED"


@pytest.mark.parametrize("body", [{"retry": "true"}, {"retry": True, "source_spans": []}, {"cypher": "RETURN 1"}])
def test_advance_contract_rejects_coercion_or_extra_fields(client, body):
    response = client.post(f"/api/v1/documents/{uuid4()}/graph-builds/{uuid4()}/advance", json=body)
    assert response.status_code == 422
