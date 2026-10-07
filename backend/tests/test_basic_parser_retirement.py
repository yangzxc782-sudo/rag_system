from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import INVALID_FILE_TYPE, BusinessError
from app.db.session import get_db
from app.main import app
from app.services import document_parsing, documents
from test_document_parsing import DOCUMENT_ID, FakeDb, fake_document


MINERU_EXTENSIONS = (".pdf", ".PDF")
RETIRED_EXTENSIONS = (".md", ".MD", ".doc", ".docx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".txt", ".csv", ".tif", ".tiff")


def unexpected_io(*args, **kwargs):
    raise AssertionError("Rejected input must not access storage or a parser")


@pytest.mark.parametrize("provider", ["basic", "simple", "unsupported"])
def test_retired_or_unknown_provider_is_rejected_at_config_load(monkeypatch, provider):
    monkeypatch.setenv("DOCUMENT_PARSER_PROVIDER", provider)
    with pytest.raises(ValidationError, match="document_parser_provider"):
        Settings(_env_file=None)


def test_supported_upload_types_cannot_be_expanded_by_legacy_env():
    settings = Settings(
        _env_file=None,
        document_parser_provider="mineru_api",
        upload_allowed_extensions=".pdf,.md,.txt,.csv,.tif,.tiff,.exe",
    )
    assert settings.upload_allowed_extension_set == {".pdf"}


def test_production_has_no_basic_modules_or_imports():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    forbidden = {"SimpleParser", "ParsedDocument", "ParsedChunk", "MinerUParser", "chunk_parsed_document"}
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                assert not any(alias.name in forbidden for alias in node.names), str(path)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                assert node.name not in forbidden, str(path)
    assert not (app_dir / "ingestion" / "chunker.py").exists()
    assert not list((app_dir / "ingestion" / "parsers").glob("*.py"))


@pytest.fixture
def isolated_routing(monkeypatch):
    document = fake_document()
    db = FakeDb(document=document)
    settings = Settings(_env_file=None, document_parser_provider="mineru_api")
    monkeypatch.setattr(document_parsing, "get_settings", lambda: settings)
    monkeypatch.setattr(documents, "get_settings", lambda: settings)
    monkeypatch.setattr(document_parsing, "get_object_bytes_from_minio", unexpected_io)
    monkeypatch.setattr(document_parsing, "_create_mineru_client", unexpected_io)
    monkeypatch.setattr(documents, "upload_bytes_to_minio", unexpected_io)
    monkeypatch.setitem(app.dependency_overrides, get_db, lambda: db)
    return document, db


@pytest.mark.parametrize("extension", RETIRED_EXTENSIONS)
def test_unsupported_upload_is_rejected_before_storage(isolated_routing, extension):
    _document, db = isolated_routing
    response = TestClient(app).post(
        "/api/v1/documents",
        files={"file": ("source" + extension, b"content", "application/octet-stream")},
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == INVALID_FILE_TYPE
    assert db.commits == 0
    assert db.added == []


@pytest.mark.parametrize("extension", RETIRED_EXTENSIONS)
def test_existing_unsupported_document_cannot_enter_mineru(isolated_routing, extension):
    document, db = isolated_routing
    document.original_filename = "source" + extension
    document.file_type = extension
    with pytest.raises(BusinessError) as caught:
        document_parsing.parse_document(db, DOCUMENT_ID)
    assert caught.value.code == INVALID_FILE_TYPE
    assert db.commits == 0
    assert document.process_status == "uploaded"


@pytest.mark.parametrize("extension", MINERU_EXTENSIONS)
def test_supported_non_markdown_document_routes_to_existing_mineru(isolated_routing, monkeypatch, extension):
    document, db = isolated_routing
    document.original_filename = "source" + extension
    document.file_type = extension
    expected = object()
    calls = []

    def mineru_route(session, source, settings, *, processing_lease=None):
        assert processing_lease is None
        calls.append((session, source, settings.document_parser_provider))
        return expected

    monkeypatch.setattr(document_parsing, "_parse_document_with_mineru", mineru_route)
    assert document_parsing.parse_document(db, DOCUMENT_ID) is expected
    assert calls == [(db, document, "mineru_api")]
