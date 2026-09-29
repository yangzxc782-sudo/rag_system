from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.v1.conversations import router
from app.local_server import LocalConversationTransport, server_config
from app.services.conversation_repository import ConversationError


def client_for(*, enabled=True, wrapped=True, peer="127.0.0.1"):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.settings = SimpleNamespace(conversation_enabled=enabled)
    app.state.conversations = Mock()
    client = TestClient(LocalConversationTransport(app) if wrapped else app,
                        base_url="http://127.0.0.1:8000", client=(peer, 12345))
    return client, app.state.conversations


def test_local_launcher_configuration():
    config = server_config(FastAPI())
    assert config.host == "127.0.0.1" and config.proxy_headers is False and config.forwarded_allow_ips == ""


@pytest.mark.parametrize("kw,code", [({"enabled": False}, "QA_FEATURE_DISABLED"),
    ({"wrapped": False}, "QA_LOCAL_TRANSPORT_REQUIRED"), ({"peer": "192.168.1.9"}, "QA_LOCAL_ACCESS_ONLY")])
def test_closed_boundary(kw, code):
    client, service = client_for(**kw)
    response = client.get("/api/v1/rag/sessions")
    assert not response.json()["success"] and response.json()["error"]["code"] == code
    assert not service.mock_calls


@pytest.mark.parametrize("headers", [{"X-Forwarded-For": "127.0.0.1"}, {"Forwarded": "for=127.0.0.1"},
    {"Host": "attacker.example"}, {"Origin": "https://attacker.example"}, {"X-Forwarded-Host": "localhost"}])
def test_headers_never_grant_local_access(headers):
    client, service = client_for()
    assert client.get("/api/v1/rag/sessions", headers=headers).status_code == 403
    assert not service.mock_calls


def test_invalid_input_envelope_hides_input():
    client, service = client_for()
    response = client.post(f"/api/v1/rag/sessions/{uuid4()}/turns", json={"question": "SECRET_INPUT", "attempt_no": 4})
    assert response.status_code == 422 and response.json()["error"]["code"] == "QA_REQUEST_INVALID"
    assert "SECRET_INPUT" not in response.text and not service.mock_calls


@pytest.mark.parametrize("error,code,status", [
    (ConversationError("QA_SESSION_NOT_FOUND", "SECRET_STACK", detail="SECRET_URL", status_code=404), "QA_SESSION_NOT_FOUND", 404),
    (ConversationError("QA_THREAD_BUSY", "secret", status_code=409), "THREAD_BUSY", 409),
    (RuntimeError("SECRET_DB"), "QA_SERVICE_UNAVAILABLE", 503)])
def test_safe_errors(error, code, status):
    client, service = client_for()
    service.list_sessions.side_effect = error
    response = client.get("/api/v1/rag/sessions")
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert "SECRET" not in response.text
