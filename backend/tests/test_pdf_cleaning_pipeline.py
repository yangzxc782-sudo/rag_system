from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.errors import BusinessError, DOCUMENT_DELETION_IN_PROGRESS, DOCUMENT_PARSE_FAILED
from app.ingestion.pdf_cleaner import render_cleaned_block
from app.models import DocumentAsset, DocumentBlock, DocumentChunk, DocumentChunkBlock, DocumentParseRun
from app.services import document_parsing
from app.services.document_deletion_manifest import build_document_deletion_manifest
from test_document_chunk_writer import db, document  # local SQLite fixtures, no external database
from test_document_parsing_mineru import (
    FakeDb, FakeMinerUClient, DOCUMENT_ID, _install_success_dependencies,
    fake_document, fake_parse_result, fake_settings,
)


def dependencies(monkeypatch, *, enabled=True):
    parsed = fake_parse_result()
    parsed = replace(parsed, content_list=(
        {"type": "title", "text": "Foreword", "page_idx": 0},
        {"type": "text", "text": "REMOVE FOREWORD", "page_idx": 0},
        {"type": "title", "text": "1 Scope", "page_idx": 1, "text_level": 3},
        {"type": "text", "text": "STALE", "markdown": "tensile\nstrength 350 MPa", "page_idx": 1, "bbox": [10, 20, 30, 40]},
        {"type": "formula", "latex": r"A \wedge B", "page_idx": 1},
        {"type": "table", "table_body": "<table><tr><td>Ca</td><td>Nb</td></tr></table>",
         "table_caption": ["Table 1"], "table_footnote": ["Measured values"], "page_idx": 1},
        {"type": "title", "text": "References", "page_idx": 2},
        {"type": "text", "text": "REMOVE REFERENCES", "page_idx": 2},
        {"type": "title", "text": "Annex A", "page_idx": 2},
        {"type": "text", "text": "450 MPa", "page_idx": 2},
    ))
    client = FakeMinerUClient(result=parsed)
    _, uploads = _install_success_dependencies(monkeypatch, client=client)
    settings = fake_settings()
    settings.pdf_cleaning_enabled = enabled
    settings.pdf_cleaning_profile = "iso_en"
    settings.pdf_cleaning_backfill_enabled = False
    settings.chunk_size_chars = 10000
    monkeypatch.setattr(document_parsing, "get_settings", lambda: settings)
    return client, uploads


def test_cleaned_markdown_is_only_new_file_and_matches_real_persisted_blocks_and_chunks(monkeypatch, db, document):
    client, uploads = dependencies(monkeypatch)
    document.original_filename = "ISO 9001.pdf"
    document.file_type = ".pdf"
    document.mime_type = "application/pdf"
    db.commit()
    original = deepcopy(client.result)
    result = document_parsing.parse_document(db, document.id)
    run = db.scalar(select(DocumentParseRun))
    blocks = list(db.scalars(select(DocumentBlock).order_by(DocumentBlock.block_index)))
    chunks = list(db.scalars(select(DocumentChunk).order_by(DocumentChunk.chunk_index)))
    assets = list(db.scalars(select(DocumentAsset)))
    mapping_count = db.scalar(select(func.count()).select_from(DocumentChunkBlock))
    files = {upload["object_key"].split("/")[-1]: upload["content"] for upload in uploads}
    assert set(files) == {"output.md", "output.json", "cleaned.md"}
    assert files["output.md"] == original.result_files[0].content
    assert files["output.json"] == original.result_files[1].content
    assert client.result == original
    assert len(client.requests) == 1
    assert [b.block_index for b in blocks] == [2, 3, 4, 5, 8, 9]
    canonical = "\n\n".join(render_cleaned_block(b, output_prefix=run.output_prefix)[0].strip() for b in blocks)
    assert files["cleaned.md"].decode() == canonical
    assert "\n\n".join(c.content for c in chunks) == canonical
    assert "STALE" not in canonical and "REMOVE" not in canonical
    assert "350 MPa" in canonical and "450 MPa" in canonical and r"\wedge" in canonical
    assert "Measured values" in canonical
    assert blocks[1].bbox == [10, 20, 30, 40]
    assert blocks[1].page_start == 1
    assert mapping_count == len(blocks)
    assert all(c.embedding_status == "not_started" for c in chunks)
    assert result.process_status == "parsed" and run.is_active
    assert run.block_count == len(blocks) and run.asset_count == len(assets) == 3
    assert next(a for a in assets if a.filename == "cleaned.md").asset_type == "markdown"
    # Existing deletion manifest covers both the new asset and run prefix, no new lifecycle.
    settings = SimpleNamespace(mineru_output_prefix="parsed-assets", search_index_name="casting_chunks_v1", search_index_alias="casting_chunks_current")
    manifest = build_document_deletion_manifest(db, document=document, settings=settings)
    assert f"{run.output_prefix}/cleaned.md" in manifest.derived_object_keys


@pytest.mark.parametrize(("extension", "enabled"), [(".pdf", False), (".docx", True), (".png", True)])
def test_disabled_and_non_pdf_paths_do_not_call_cleaner(monkeypatch, extension, enabled):
    client, uploads = dependencies(monkeypatch, enabled=enabled)
    item = fake_document()
    item.original_filename = f"input{extension}"
    item.file_type = extension
    monkeypatch.setattr(document_parsing, "clean_pdf_blocks", lambda *a, **kw: pytest.fail("unexpected cleaner"))
    document_parsing.parse_document(FakeDb(document=item), DOCUMENT_ID)
    assert len(client.requests) == 1
    assert len(uploads) == 2


@pytest.mark.parametrize("failure", ["cleaner", "upload", "chunk_write", "commit"])
def test_cleaning_pipeline_failure_rolls_back_all_chunks_without_new_recovery_state(monkeypatch, db, document, failure):
    client, uploads = dependencies(monkeypatch)
    document.original_filename = "input.pdf"
    document.file_type = ".pdf"
    db.commit()

    def fail(*args, **kwargs):
        raise RuntimeError("private-original-text")

    if failure == "cleaner":
        monkeypatch.setattr(document_parsing, "clean_pdf_blocks", fail)
    elif failure == "upload":
        def upload(**kwargs):
            if kwargs["object_key"].endswith("/cleaned.md"):
                fail()
            uploads.append(kwargs)
        monkeypatch.setattr(document_parsing, "upload_bytes_to_minio", upload)
    elif failure == "chunk_write":
        monkeypatch.setattr(document_parsing, "_add_mineru_chunks", fail)
    else:
        commit = db.commit
        calls = 0
        def fail_final_commit():
            nonlocal calls
            calls += 1
            if calls == 2:
                fail()
            return commit()
        monkeypatch.setattr(db, "commit", fail_final_commit)
    with pytest.raises(BusinessError) as error:
        document_parsing.parse_document(db, document.id)
    assert error.value.code == DOCUMENT_PARSE_FAILED
    assert len(client.requests) == 1
    for model in (DocumentBlock, DocumentAsset, DocumentChunk, DocumentChunkBlock):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    run = db.scalar(select(DocumentParseRun))
    assert run.status == "failed" and not run.is_active
    assert document.process_status == "parse_failed"
    assert "private-original-text" not in run.error_message
    if failure == "cleaner":
        assert uploads == []


def test_delete_during_cleaning_blocks_all_final_uploads(monkeypatch):
    client, uploads = dependencies(monkeypatch)
    item = fake_document()
    actual = document_parsing.clean_pdf_blocks
    def delete_during_cleaning(*args, **kwargs):
        result = actual(*args, **kwargs)
        item.deletion_status = "deleting"
        return result
    monkeypatch.setattr(document_parsing, "clean_pdf_blocks", delete_during_cleaning)
    db = FakeDb(document=item)
    with pytest.raises(BusinessError) as error:
        document_parsing.parse_document(db, DOCUMENT_ID)
    assert error.value.code == DOCUMENT_DELETION_IN_PROGRESS
    assert uploads == [] and db.added_all == []
    assert len(client.requests) == 1


def test_cleaned_key_cannot_overwrite_mineru_asset(monkeypatch):
    client, uploads = dependencies(monkeypatch)
    from app.ingestion.mineru.models import MinerUResultFile
    client.result = replace(client.result, result_files=client.result.result_files + (
        MinerUResultFile(file_type="other", filename="cleaned.md", source_path="cleaned.md", content=b"original"),
    ))
    db = FakeDb(document=fake_document())
    with pytest.raises(BusinessError):
        document_parsing.parse_document(db, DOCUMENT_ID)
    assert uploads == []
