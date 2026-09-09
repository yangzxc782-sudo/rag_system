from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app
from app.models import Document, DocumentAsset, DocumentBlock, DocumentChunk, DocumentChunkBlock, DocumentParseRun, DocumentDeletionJob
from app.services import document_parsing
from app.services.document_deletion import ClaimedDocumentDeletion, finalize_postgresql_deletion, request_document_deletion
from app.services.document_deletion_manifest import DocumentDeletionManifest, build_document_deletion_manifest
from test_document_chunk_writer import db, document, count  # isolated ORM fixtures
from test_kg_anchor_parser import start, end


SOURCE = "# 工艺\n\n" + start("A") + start("B", anchor_type="table", table_line="table_ref: T1\n") + "| Si | wt.% |\n| --- | --- |\n| ≥6.50 | ≤7.50 |\n" + end("B") + end("A")


def unexpected(*args, **kwargs):
    raise AssertionError("Markdown must not invoke MinerU or write derived storage")


@pytest.fixture
def native(monkeypatch, db, document):
    settings = SimpleNamespace(document_parser_provider="mineru_api", chunk_size_chars=60, chunk_overlap_chars=0,
        mineru_output_prefix="parsed-assets", search_index_name="m3-test-chunks-v1", search_index_alias="m3-test-chunks-current",
        document_deletion_executor_enabled=True, document_deletion_max_step_attempts=5)
    state = SimpleNamespace(content=SOURCE.encode("utf-8"), reads=[], settings=settings)

    def read(**kwargs):
        state.reads.append(kwargs)
        return state.content

    monkeypatch.setattr(document_parsing, "get_settings", lambda: settings)
    monkeypatch.setattr(document_parsing, "get_object_bytes_from_minio", read)
    for name in ("_create_mineru_client", "_parse_document_with_mineru", "upload_bytes_to_minio", "create_parse_run", "add_document_assets", "add_document_blocks"):
        monkeypatch.setattr(document_parsing, name, unexpected)
    monkeypatch.setitem(app.dependency_overrides, get_db, lambda: db)
    return state


def post(document):
    return TestClient(app).post(f"/api/v1/documents/{document.id}/parse")


@pytest.mark.parametrize("extension,encoding", [(".md", "utf-8"), (".MD", "utf-8-sig")])
def test_production_markdown_success_never_mineru_and_preserves_metadata(db, document, native, extension, encoding):
    document.original_filename = "source" + extension
    db.commit()
    native.content = SOURCE.encode(encoding)
    response = post(document)
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload == {"document_id": str(document.id), "process_status": "parsed", "chunk_count": 2,
                       "parser_name": "markdown_native", "parser_version": "markdown-it-py/4.2.0"}
    assert native.reads == [{"bucket_name": document.bucket_name, "object_key": document.object_key}]
    chunks = list(db.scalars(select(DocumentChunk).order_by(DocumentChunk.chunk_index)))
    assert len(chunks) == 2
    for chunk in chunks:
        assert chunk.parse_run_id is None and chunk.block_mappings == []
        assert chunk.chunk_method == "markdown_ast" and chunk.content_format == "markdown"
        assert chunk.embedding_status == "not_started" and chunk.embedding is None
        metadata = chunk.source_metadata
        assert metadata["parser_provider"] == "markdown_native"
        assert metadata["parser_version"] == payload["parser_version"]
        assert metadata["section_path"] == ["工艺"]
        assert metadata["source_range"]["kind"] == "markdown_ast"
        assert metadata["source_range"]["ast_node_ids"]
        assert metadata["source_range"]["start_line"] >= 1
        for marker in ("kg-anchor-start", "kg-anchor-end", "anchor_id:", "graph_id:", "anchor_type:", "table_ref:"):
            assert marker not in chunk.content
    assert chunks[1].source_metadata["kg_refs"] == [
        {"anchor_id": "A", "graph_id": "G::2026", "anchor_type": "section", "table_ref": None},
        {"anchor_id": "B", "graph_id": "G::2026", "anchor_type": "table", "table_ref": "T1"}]
    for model in (DocumentParseRun, DocumentBlock, DocumentAsset, DocumentChunkBlock):
        assert count(db, model) == 0


def test_chunk_and_detail_apis_accept_native_document_without_run(db, document, native):
    assert post(document).status_code == 200
    client = TestClient(app)
    base = f"/api/v1/documents/{document.id}"
    assert client.get(base).json()["data"]["process_status"] == "parsed"
    chunk_response = client.get(base + "/chunks")
    assert chunk_response.status_code == 200
    assert chunk_response.json()["data"]["items"][1]["source_metadata"]["kg_refs"][0]["table_ref"] is None
    status = client.get(base + "/parse-status").json()["data"]
    assert status["process_status"] == "parsed"
    assert status["latest_parse_run"] is status["active_parse_run"] is None
    for route in ("parse-runs", "blocks", "assets"):
        result = client.get(base + "/" + route)
        assert result.status_code == 200 and result.json()["data"]["items"] == []


@pytest.mark.parametrize("source,reason", [(start().encode(), "ANCHOR_UNCLOSED"), (b"\xff\xfe", "MARKDOWN_ENCODING_INVALID")])
def test_invalid_source_returns_safe_400_and_no_chunks_then_can_retry(db, document, native, source, reason):
    native.content = source
    response = post(document)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "DOCUMENT_MARKDOWN_INVALID"
    assert error["detail"]["reason"] == reason
    assert set(error["detail"]) <= {"reason", "line", "anchor_id"}
    assert count(db, DocumentChunk) == count(db, DocumentParseRun) == 0
    assert document.process_status == "parse_failed"
    assert "kg-anchor" not in (document.error_message or "")
    native.content = b"Corrected content\n"
    assert post(document).status_code == 200
    assert document.error_message is None


@pytest.mark.parametrize("source", [b"Body without anchors\n", b"", (start() + end()).encode()])
def test_plain_and_empty_markdown_do_not_create_fake_provenance(db, document, native, source):
    native.content = source
    response = post(document)
    assert response.status_code == 200
    assert count(db, DocumentChunk) == (1 if source.startswith(b"Body") else 0)
    assert all(chunk.source_metadata["kg_refs"] == [] for chunk in db.scalars(select(DocumentChunk)))
    assert count(db, DocumentParseRun) == count(db, DocumentBlock) == count(db, DocumentAsset) == 0


@pytest.mark.parametrize("status,code", [("deleting", "DOCUMENT_DELETION_IN_PROGRESS"), ("delete_failed", "DOCUMENT_DELETE_FAILED")])
def test_initial_guard_rejects_deleted_state_before_source_read(db, document, native, status, code):
    document.deletion_status = status
    db.commit()
    response = post(document)
    assert response.status_code == 409 and response.json()["error"]["code"] == code
    assert native.reads == [] and count(db, DocumentChunk) == 0


def test_delete_committed_after_chunking_prevents_final_write(db, document, native, monkeypatch):
    from app.ingestion.markdown.chunker import build_markdown_chunks
    snapshots = []

    def delete_after_chunking(*args, **kwargs):
        drafts = build_markdown_chunks(*args, **kwargs)
        with Session(db.get_bind()) as deleter:
            request_document_deletion(deleter, document.id, settings=native.settings)
            snapshots.append(deleter.scalar(select(DocumentDeletionJob)).manifest)
        return drafts

    monkeypatch.setattr(document_parsing, "build_markdown_chunks", delete_after_chunking, raising=False)
    response = post(document)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DOCUMENT_DELETION_IN_PROGRESS"
    assert snapshots[0]["chunk_ids"] == []
    assert count(db, DocumentChunk) == 0
    assert document.deletion_status == "deleting"
    locks = [item for item in db.events if isinstance(item, tuple) and item[0] == "lock"]
    assert len(locks) >= 2 and all(item[2].get("populate_existing") for item in locks)


def test_markdown_write_commits_before_delete_manifest_captures_chunks(db, document, native):
    assert post(document).status_code == 200
    chunk_ids = {str(item.id) for item in db.scalars(select(DocumentChunk))}
    with Session(db.get_bind()) as deleter:
        request_document_deletion(deleter, document.id, settings=native.settings)
        manifest = deleter.scalar(select(DocumentDeletionJob)).manifest
    assert set(manifest["chunk_ids"]) == chunk_ids
    for key in ("parse_run_ids", "block_ids", "asset_ids", "derived_prefixes", "derived_object_keys"):
        assert manifest[key] == []
    assert manifest["raw_object_key"] == document.object_key


def test_markdown_writer_runs_under_final_guard_without_intermediate_commit(db, document, native, monkeypatch):
    from app.services.document_chunk_writer import DocumentChunkWriter
    original = DocumentChunkWriter.write
    observed = []

    def checked(writer, **kwargs):
        assert isinstance(db.events[-1], tuple) and db.events[-1][0] == "lock"
        assert db.in_transaction()
        commits = db.commits
        result = original(writer, **kwargs)
        assert db.commits == commits
        observed.append(result)
        return result

    monkeypatch.setattr(DocumentChunkWriter, "write", checked)
    assert post(document).status_code == 200
    assert observed


@pytest.mark.parametrize("stage", ["chunking", "writer", "commit"])
def test_native_failures_rollback_all_chunks_and_expose_only_safe_error(db, document, native, monkeypatch, stage):
    secret = "private full source and internal path"
    if stage == "chunking":
        def fail(*args, **kwargs):
            raise RuntimeError(secret)
        monkeypatch.setattr(document_parsing, "build_markdown_chunks", fail, raising=False)
    else:
        fired = []
        def fail(session, *args):
            has_chunks = (
                session.connection().scalar(select(DocumentChunk.id).limit(1)) is not None
                if stage == "commit"
                else any(isinstance(item, DocumentChunk) for item in session.new)
            )
            if not fired and has_chunks:
                fired.append(True)
                raise RuntimeError(secret)
        event.listen(db, "after_flush" if stage == "writer" else "before_commit", fail)
    try:
        response = post(document)
    finally:
        if stage != "chunking":
            event.remove(db, "after_flush" if stage == "writer" else "before_commit", fail)
    assert response.status_code == 500 and response.json()["error"]["code"] == "DOCUMENT_PARSE_FAILED"
    assert secret not in response.text and secret not in (document.error_message or "")
    assert count(db, DocumentChunk) == count(db, DocumentChunkBlock) == 0
    assert document.process_status == "parse_failed"


def test_repeat_parse_does_not_read_replace_or_rechunk(db, document, native):
    assert post(document).status_code == 200
    before = [(item.id, deepcopy(item.source_metadata)) for item in db.scalars(select(DocumentChunk))]
    response = post(document)
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_ALREADY_PARSED"
    assert len(native.reads) == 1
    assert [(item.id, item.source_metadata) for item in db.scalars(select(DocumentChunk))] == before
    assert document.process_status == "parsed"


def test_other_parse_wins_during_chunking_final_recheck_preserves_winner(db, document, native, monkeypatch):
    from app.ingestion.markdown.chunker import build_markdown_chunks
    from app.services.document_chunk_writer import DocumentChunkWriter
    from app.services.document_operation_guard import DocumentOperationGuard
    winner_ids = []

    def winner(*args, **kwargs):
        drafts = build_markdown_chunks(*args, **kwargs)
        with Session(db.get_bind()) as other:
            locked = DocumentOperationGuard(other).lock_normal(document.id)
            saved = DocumentChunkWriter(other).write(document_id=document.id, drafts=drafts)
            winner_ids.extend(item.id for item in saved)
            locked.process_status = "parsed"
            other.commit()
        return drafts

    monkeypatch.setattr(document_parsing, "build_markdown_chunks", winner, raising=False)
    response = post(document)
    assert response.status_code == 409 and response.json()["error"]["code"] == "DOCUMENT_ALREADY_PARSED"
    assert {item.id for item in db.scalars(select(DocumentChunk))} == set(winner_ids)
    assert document.process_status == "parsed"


def test_existing_hard_delete_finalization_deletes_native_chunks_document_then_job(db, document, native):
    assert post(document).status_code == 200
    identity = document.id
    manifest = build_document_deletion_manifest(db, document=document, settings=native.settings)
    assert manifest.chunk_ids and manifest.parse_run_ids == manifest.block_ids == manifest.asset_ids == ()
    request_document_deletion(db, identity, settings=native.settings)
    job = db.scalar(select(DocumentDeletionJob))
    job.status = "processing"
    job.current_step = "finalize_postgresql"
    job.lease_token = uuid4()
    job.locked_by = "m3-unit-test"
    job.locked_at = datetime.now(timezone.utc)
    job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    db.commit()
    claimed = ClaimedDocumentDeletion.from_job(job)
    statements = []

    def record(connection, cursor, sql, parameters, context, executemany):
        if sql.startswith("DELETE"):
            statements.append(sql)

    event.listen(db.get_bind(), "before_cursor_execute", record)
    try:
        finalize_postgresql_deletion(db, claimed=claimed, manifest=DocumentDeletionManifest.from_payload(job.manifest))
        db.commit()
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", record)
    assert count(db, DocumentChunk) == count(db, DocumentChunkBlock) == count(db, Document) == count(db, DocumentDeletionJob) == 0
    assert statements[-1].startswith("DELETE FROM document_deletion_jobs")
    assert next(i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM document_chunks ")) < next(i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM documents "))
