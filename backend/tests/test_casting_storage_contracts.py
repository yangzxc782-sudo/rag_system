"""Offline migration, local transport, bounded upload and error contracts."""
import importlib.util
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from contextlib import nullcontext
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.v1.casting_design import MAX_MULTIPART_BYTES, router
from app.core.config import Settings
from app.local_server import LocalConversationTransport
from app.schemas.casting_storage import CastingStorageError
from app.schemas.conversations import TurnCreateRequest
from app.services.conversation_repository import fingerprint, turn_fingerprint
from app.services.conversations import Conversations


def test_migration_additive_offline_and_refuses_destructive_downgrade():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0011_casting_storage.py"
    spec = importlib.util.spec_from_file_location("casting_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        module.upgrade()
    sql = output.getvalue()
    assert module.down_revision == "0010_phase13_checkpoints"
    for forbidden in ("DROP ", "DELETE ", "TRUNCATE ", "UPDATE "):
        assert forbidden not in sql
    assert "fk_casting_runs_turn" in sql and "fk_casting_runs_result" in sql
    with pytest.raises(RuntimeError, match="retain"):
        module.downgrade()


def test_request_fingerprint_keeps_legacy_identity_but_binds_explicit_input():
    did, fid = uuid4(), uuid4()
    assert turn_fingerprint("q", 8, did) == fingerprint({"question": "q", "limit": 8, "document_id": str(did)})
    assert turn_fingerprint("q", 8, did, fid) != turn_fingerprint("q", 8, did)
    assert turn_fingerprint("q", 8, did, fid) != turn_fingerprint("q", 8, did, uuid4())


def test_feature_default_and_chat_attachment_does_not_silently_use_rag():
    assert Settings(_env_file=None).casting_design_enabled is False
    with pytest.raises(ValueError):
        Settings(_env_file=None, casting_design_enabled=True)
    service = object.__new__(Conversations)
    service.settings = Settings(_env_file=None)
    service.session_factory = lambda: nullcontext(SimpleNamespace(begin=lambda: nullcontext()))
    service._validate_request = lambda *a: None
    with pytest.raises(Exception) as caught:
        service.submit(uuid4(), TurnCreateRequest(request_id=uuid4(), question="计算", casting_input_file_id=uuid4()))
    assert caught.value.code == "CASTING_FEATURE_DISABLED"


def api():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.settings = SimpleNamespace(conversation_enabled=True, casting_design_enabled=True)
    app.state.conversations = Mock()
    app.state.casting_design = Mock()
    return TestClient(LocalConversationTransport(app), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 1234)), app


def test_upload_without_content_length_is_bounded_before_multipart_parse():
    client, app = api()
    chunks = [b"--boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"in.json\"\r\n\r\n",
              b"x" * (MAX_MULTIPART_BYTES + 1), b"\r\n--boundary--\r\n"]
    result = client.post(f"/api/v1/rag/sessions/{uuid4()}/casting-inputs", content=iter(chunks),
                        headers={"Content-Type": "multipart/form-data; boundary=boundary"})
    assert result.status_code == 413 and result.json()["error"]["code"] == "CASTING_FILE_TOO_LARGE"
    assert not app.state.casting_design.mock_calls


def test_safe_field_errors_preserved_but_internal_exception_hidden():
    client, app = api()
    app.state.casting_design.files.list_inputs.side_effect = CastingStorageError(
        "CASTING_INPUT_INVALID", "结构检查失败", status=422, category="file",
        issues=[{"field_path": "input.hotspots", "error_code": "missing", "message": "缺少热节数组"}])
    response = client.get(f"/api/v1/rag/sessions/{uuid4()}/casting-inputs")
    assert response.json()["error"]["detail"]["issues"][0]["field_path"] == "input.hotspots"
    app.state.casting_design.files.list_inputs.side_effect = RuntimeError("PRIVATE_OBJECT_PATH")
    response = client.get(f"/api/v1/rag/sessions/{uuid4()}/casting-inputs")
    assert response.status_code == 503 and "PRIVATE" not in response.text
