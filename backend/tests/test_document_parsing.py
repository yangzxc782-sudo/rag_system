from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.core.errors import (
    DOCUMENT_ALREADY_PARSED,
    DOCUMENT_NOT_FOUND,
    DOCUMENT_PARSE_FAILED,
    DOCUMENT_PARSER_CONFIG_INVALID,
    DOCUMENT_SOURCE_FILE_NOT_FOUND,
    DOCUMENT_DELETION_IN_PROGRESS,
    BusinessError,
)
from app.services import document_parsing


DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


class FakeScalarResult:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class FakeDb:
    def __init__(
        self,
        *,
        document=None,
        chunk_count: int = 0,
        chunks=None,
        fail_commit_after_add_all: bool = False,
    ) -> None:
        self.document = document
        self.chunk_count = chunk_count
        self.chunks = chunks or []
        self.fail_commit_after_add_all = fail_commit_after_add_all
        self.failed_commit_once = False
        self.added = []
        self.added_all = []
        self.commits = 0
        self.rollbacks = 0
        self.refreshed = []

    def get(self, model, item_id):
        return self.document

    def scalar(self, statement):
        if "FROM documents" in str(statement):
            return self.document
        if self.chunks:
            return len(self.chunks)
        return self.chunk_count

    def scalars(self, statement):
        return FakeScalarResult(sorted(self.chunks, key=lambda chunk: chunk.chunk_index))

    def add(self, item):
        self.added.append(item)

    def add_all(self, items):
        self.added_all.extend(items)

    def commit(self):
        self.commits += 1
        if self.fail_commit_after_add_all and self.added_all and not self.failed_commit_once:
            self.failed_commit_once = True
            raise RuntimeError("simulated chunk write failure")

    def rollback(self):
        self.rollbacks += 1

    def refresh(self, item):
        self.refreshed.append(item)


def fake_document(process_status: str = "uploaded") -> SimpleNamespace:
    return SimpleNamespace(
        id=DOCUMENT_ID,
        original_filename="notes.pdf",
        bucket_name="rag-documents",
        object_key=f"raw/2026/07/{DOCUMENT_ID}.pdf",
        file_type=".pdf",
        mime_type="text/plain",
        process_status=process_status,
        deletion_status="normal",
        error_message=None,
    )


def fake_settings(
    document_parser_provider: str = "mineru_api",
) -> SimpleNamespace:
    return SimpleNamespace(
        document_parser_provider=document_parser_provider,
        chunk_size_chars=1000,
        chunk_overlap_chars=100,
        mineru_api_base_url=None,
        mineru_api_key=None,
        mineru_api_timeout_seconds=300,
        mineru_api_poll_interval_seconds=5,
        mineru_api_max_poll_attempts=120,
        mineru_output_prefix="parsed-assets",
        mineru_parse_mode="auto",
        mineru_enable_ocr=True,
        mineru_save_intermediate=True,
    )


def test_parse_document_success_uses_supported_mineru_route(monkeypatch) -> None:
    from test_document_parsing_mineru import FakeDb as MinerUDb, _install_success_dependencies

    document = fake_document()
    db = MinerUDb(document=document)
    _install_success_dependencies(monkeypatch)
    result = document_parsing.parse_document(db, DOCUMENT_ID)
    assert result.document_id == DOCUMENT_ID
    assert result.process_status == "cleaned_source_ready"
    assert result.chunk_count == 0
    assert result.parser_name == "mineru_api"
    assert document.process_status == "cleaned_source_ready"


def test_parse_deleting_document_never_reads_or_persists_source(monkeypatch) -> None:
    document = fake_document()
    document.deletion_status = "deleting"
    db = FakeDb(document=document)
    monkeypatch.setattr(document_parsing, "get_settings", lambda: fake_settings())

    def unexpected_read(**kwargs):
        raise AssertionError("deleting document must not start storage reads")

    monkeypatch.setattr(
        document_parsing,
        "get_object_bytes_from_minio",
        unexpected_read,
    )

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_DELETION_IN_PROGRESS
    assert db.added_all == []


def test_parse_document_not_found() -> None:
    db = FakeDb(document=None)

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_NOT_FOUND


def test_parse_document_already_has_chunks_does_not_change_status(monkeypatch) -> None:
    document = fake_document(process_status="uploaded")
    db = FakeDb(document=document, chunk_count=2)
    minio_called = False

    def fake_get_object_bytes_from_minio(**kwargs):
        nonlocal minio_called
        minio_called = True
        return b"content"

    monkeypatch.setattr(document_parsing, "get_object_bytes_from_minio", fake_get_object_bytes_from_minio)

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_ALREADY_PARSED
    assert document.process_status == "uploaded"
    assert db.added_all == []
    assert db.commits == 0
    assert minio_called is False


def test_parse_document_unknown_provider_is_rejected_before_storage(monkeypatch) -> None:
    document = fake_document()
    db = FakeDb(document=document)
    storage_called = False

    def fake_get_object_bytes_from_minio(**kwargs):
        nonlocal storage_called
        storage_called = True
        return b"%PDF"

    monkeypatch.setattr(
        document_parsing,
        "get_settings",
        lambda: fake_settings("unsupported"),
    )
    monkeypatch.setattr(
        document_parsing,
        "get_object_bytes_from_minio",
        fake_get_object_bytes_from_minio,
    )

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_PARSER_CONFIG_INVALID
    assert db.rollbacks == 0
    assert document.process_status == "uploaded"
    assert storage_called is False


def test_parse_document_source_file_not_found_marks_parse_failed(monkeypatch) -> None:
    from test_document_parsing_mineru import FakeDb as MinerUDb
    from app.services import document_processing
    monkeypatch.setattr(document_processing, "reject_pending_parse", lambda *args: None)

    monkeypatch.setattr(document_parsing, "require_source_schema", lambda db: None)
    document = fake_document()
    db = MinerUDb(document=document)
    monkeypatch.setattr(document_parsing, "get_settings", lambda: fake_settings())

    def missing_source(**kwargs):
        raise BusinessError(DOCUMENT_SOURCE_FILE_NOT_FOUND, "source missing", status_code=503)

    monkeypatch.setattr(document_parsing, "get_object_bytes_from_minio", missing_source)
    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)
    assert exc_info.value.code == DOCUMENT_SOURCE_FILE_NOT_FOUND
    assert db.rollbacks == 1
    assert document.process_status == "parse_failed"
    assert db.added_all == []


def test_parse_document_block_write_failure_rolls_back_and_marks_failed(monkeypatch) -> None:
    from app.models.document_block import DocumentBlock
    from test_document_parsing_mineru import FakeDb as MinerUDb, _install_success_dependencies

    class BlockWriteFailureDb(MinerUDb):
        def flush(self):
            if any(isinstance(item, DocumentBlock) for item in self.added_all):
                raise RuntimeError("simulated block write failure")
            super().flush()

    document = fake_document()
    db = BlockWriteFailureDb(document=document)
    _install_success_dependencies(monkeypatch)
    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)
    assert exc_info.value.code == DOCUMENT_PARSE_FAILED
    assert db.rollbacks == 1
    assert document.process_status == "parse_failed"
    assert document.error_message
    assert next(iter(db.parse_runs.values())).status == "failed"


def test_list_document_chunks_returns_stats_without_token_count() -> None:
    document = fake_document(process_status="parsed")
    chunks = [
        SimpleNamespace(
            chunk_index=1,
            content="fallback",
            source_metadata={},
            token_count=999,
        ),
        SimpleNamespace(
            chunk_index=0,
            content="abc",
            source_metadata={"character_count": 3},
            token_count=None,
        ),
    ]
    db = FakeDb(document=document, chunks=chunks)

    result = document_parsing.list_document_chunks(db, DOCUMENT_ID, limit=50, offset=0)

    assert [chunk.chunk_index for chunk in result.items] == [0, 1]
    assert result.total == 2
    assert result.limit == 50
    assert result.offset == 0
    assert result.stats["chunk_count"] == 2
    assert result.stats["total_characters"] == 11
    assert result.stats["min_characters"] == 3
    assert result.stats["max_characters"] == 8
    assert result.stats["avg_characters"] == 5.5
