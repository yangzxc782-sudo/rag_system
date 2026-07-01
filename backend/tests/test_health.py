from fastapi.testclient import TestClient

from app.api.v1 import health as health_api
from app.main import app


client = TestClient(app)


def test_root_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "success" not in body
    assert "data" not in body
    assert "error" not in body


def test_api_v1_health() -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "success" not in body
    assert "data" not in body
    assert "error" not in body


def test_api_v1_service_health_keeps_original_shape(monkeypatch) -> None:
    monkeypatch.setattr(health_api, "check_postgresql", lambda: {"status": "ok", "detail": "ok"})
    monkeypatch.setattr(health_api, "check_redis", lambda: {"status": "ok", "detail": "ok"})
    monkeypatch.setattr(
        health_api,
        "check_minio",
        lambda: {"status": "ok", "detail": "ok", "bucket": "rag-documents"},
    )

    response = client.get("/api/v1/health/services")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "services" in body
    assert "success" not in body
    assert "data" not in body
    assert "error" not in body
