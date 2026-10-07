"""M1 IO boundaries using local SQLite and explicit storage/parser doubles."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.models import SourceDocumentVersion, DocumentChunk, DocumentBlock
from app.services import document_deletion, document_parsing, documents
from app.services.document_sources import require_source_schema
from test_document_chunk_writer import db, document
from test_document_parsing_mineru import _install_success_dependencies


@pytest.mark.parametrize("filename,content", [("x.md", b"%PDF-1.4"), ("x.docx", b"%PDF-1.4"),
                                             ("x.pdf", b"# Markdown"), ("x.PDF", b"\x89PNG")])
def test_backend_rejects_non_pdf_even_with_broadened_settings(monkeypatch, filename, content):
    monkeypatch.setattr(documents, "get_settings", lambda: SimpleNamespace(
        upload_allowed_extension_set={".pdf", ".md", ".docx"}, upload_allowed_content_type_set=set(),
        upload_max_file_size_bytes=9999,
    ))
    upload = Mock(side_effect=AssertionError("rejected files must not write storage"))
    monkeypatch.setattr(documents, "upload_bytes_to_minio", upload)
    session = Mock()
    with pytest.raises(BusinessError) as error:
        documents.create_document_from_upload(session, original_filename=filename, content=content)
    assert error.value.code == "INVALID_FILE_TYPE"
    assert not upload.called and not session.mock_calls


def test_missing_schema_is_explicit_and_never_implicitly_migrates():
    engine = create_engine("sqlite://")
    try:
        with Session(engine) as session, pytest.raises(BusinessError) as error:
            require_source_schema(session)
        assert error.value.code == "DOCUMENT_SOURCE_SCHEMA_UNAVAILABLE"
    finally:
        engine.dispose()


def test_pdf_upload_with_generic_mime_uses_only_pdf_product_route(monkeypatch):
    monkeypatch.setattr(documents, "get_settings", lambda: SimpleNamespace(
        upload_allowed_extension_set={".pdf"}, upload_allowed_content_type_set=set(),
        upload_max_file_size_bytes=9999, minio_bucket="isolated-fixture",
    ))
    upload, session = Mock(), Mock()
    monkeypatch.setattr(documents, "upload_bytes_to_minio", upload)
    result = documents.create_document_from_upload(session, original_filename="规范.PDF",
                                                  content=b"%PDF-1.4 fixture", content_type="application/octet-stream")
    assert result.file_type == ".pdf" and result.object_key.endswith(".pdf")
    upload.assert_called_once()
    session.commit.assert_called_once()


def test_pdf_pipeline_has_no_sql_transaction_during_external_io_and_no_chunks(db, document, monkeypatch):
    did = document.id
    client, uploads = _install_success_dependencies(monkeypatch)
    original_parse, original_read, original_upload = (
        client.parse_file, document_parsing.get_object_bytes_from_minio, document_parsing.upload_bytes_to_minio,
    )
    def outside_sql(call):
        def wrapped(*args, **kwargs):
            assert not db.in_transaction(), "network work must not retain a SQL transaction"
            return call(*args, **kwargs)
        return wrapped
    monkeypatch.setattr(client, "parse_file", outside_sql(original_parse))
    monkeypatch.setattr(document_parsing, "get_object_bytes_from_minio", outside_sql(original_read))
    monkeypatch.setattr(document_parsing, "upload_bytes_to_minio", outside_sql(original_upload))
    result = document_parsing.parse_document(db, did)
    source = db.get(SourceDocumentVersion, result.source_version)
    assert source.character_count == result.character_count and result.chunk_count == 0
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0
    assert db.scalar(select(func.count()).select_from(DocumentBlock)) > 0
    assert result.process_status == "cleaned_source_ready"
    count_before = len(uploads)
    with pytest.raises(BusinessError) as error:
        document_parsing.parse_document(db, did)
    assert error.value.code == "DOCUMENT_ALREADY_PARSED"
    assert len(client.requests) == 1 and len(uploads) == count_before


def test_corrupt_storage_readback_never_publishes_a_source(db, document, monkeypatch):
    client, uploads = _install_success_dependencies(monkeypatch)
    read = document_parsing.get_object_bytes_from_minio
    monkeypatch.setattr(document_parsing, "get_object_bytes_from_minio", lambda **kwargs:
                        b"tampered" if kwargs["object_key"].endswith("/cleaned.md") else read(**kwargs))
    with pytest.raises(BusinessError):
        document_parsing.parse_document(db, document.id)
    assert document.process_status == "parse_failed"
    for model in (SourceDocumentVersion, DocumentBlock, DocumentChunk):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    assert len(client.requests) == 1 and uploads


@pytest.mark.parametrize("process_status,code", [("parsing", "DOCUMENT_PROCESSING_IN_PROGRESS"),
                                              ("cleaned_source_ready", "DOCUMENT_FROZEN_SOURCE_PROTECTED")])
def test_delete_rejected_before_manifest_or_external_work(monkeypatch, process_status, code):
    item = SimpleNamespace(id=uuid4(), process_status=process_status, deletion_status="normal")
    monkeypatch.setattr(document_deletion, "_load_locked_pair_by_document_id", lambda *args: (item, None))
    manifest = Mock(side_effect=AssertionError("must reject before manifest"))
    monkeypatch.setattr(document_deletion, "build_document_deletion_manifest", manifest)
    session = Mock()
    with pytest.raises(BusinessError) as error:
        document_deletion.request_document_deletion(session, item.id, settings=SimpleNamespace(document_deletion_executor_enabled=True))
    assert error.value.code == code and not manifest.called
    session.rollback.assert_called_once()
    session.add.assert_not_called()


def test_markdown_native_source_and_imports_retired_but_pdf_artifacts_remain():
    root = Path(__file__).resolve().parents[1] / "app"
    assert not list((root / "ingestion/markdown").glob("*.py"))
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("app.ingestion.markdown")
    tree = ast.parse((root / "services/document_parsing.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names.intersection({"DocumentChunkWriter", "build_block_aware_chunks", "build_markdown_chunks"})
