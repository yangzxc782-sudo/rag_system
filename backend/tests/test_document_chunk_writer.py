from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import CheckConstraint, JSON, DefaultClause, MetaData, create_engine, event, func, select, text
from sqlalchemy.dialects.postgresql import JSONB, dialect
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import Document, DocumentBlock, DocumentChunk, DocumentChunkBlock, DocumentParseRun
from app.ingestion.chunk_drafts import ChunkBlockRef
from pdf_source_fixtures import pdf_draft


class TrackedSession(Session):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commits = 0
        self.events = []

    def commit(self):
        super().commit()
        self.commits += 1
        self.events.append("commit")

    def scalar(self, statement, *args, **kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            sql = str(statement.compile(dialect=dialect()))
            assert "FOR UPDATE" in sql
            self.events.append(("lock", sql, statement.get_execution_options()))
        return super().scalar(statement, *args, **kwargs)


@pytest.fixture
def db():
    # Local ORM/transaction tests only. Clone DDL types for SQLite; never mutate
    # production metadata or claim this verifies PostgreSQL lock contention.
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    local = MetaData()
    # This fixture verifies the unchanged legacy writer/cleaner, not M0's
    # PostgreSQL-specific version guards. Those have a separately gated suite.
    m0_tables = {"document_graph_builds", "kg_extraction_units",
                 "document_chunk_sets", "document_processing_jobs"}
    m0_constraints = {"fk_documents_current_chunk_set", "fk_document_chunks_set_owner",
                      "fk_document_chunks_source_parse", "ck_document_chunks_source_contract"}
    for table in Base.metadata.sorted_tables:
        if table.name in m0_tables:
            continue
        copied = table.to_metadata(local)
        for constraint in tuple(copied.constraints):
            if constraint.name in m0_constraints or (table.name == "document_source_versions" and isinstance(constraint, CheckConstraint)):
                copied.constraints.remove(constraint)
                for foreign_key in getattr(constraint, "elements", ()):
                    copied.foreign_keys.discard(foreign_key)
                    foreign_key.parent.foreign_keys.discard(foreign_key)
        for column in copied.columns:
            if isinstance(column.type, (JSONB, VECTOR)):
                column.type = JSON()
            if column.server_default is not None and "::jsonb" in str(column.server_default.arg):
                column.server_default = DefaultClause(text("'{}'"))
    local.create_all(engine)
    with TrackedSession(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def document(db):
    identity = uuid4()
    item = Document(id=identity, original_filename="standard.pdf", file_type=".pdf",
                    mime_type="application/pdf", bucket_name="m3-test-documents",
                    object_key=f"raw/2026/09/{identity}.pdf", process_status="uploaded")
    db.add(item)
    db.commit()
    return item


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def write(db, document_id, drafts):
    from app.services.document_chunk_writer import DocumentChunkWriter

    return DocumentChunkWriter(db).write(document_id=document_id, drafts=drafts)


def linked_drafts(db, document):
    """Synthetic neutral drafts for the shared writer, not a second PDF path."""
    run = DocumentParseRun(id=uuid4(), document_id=document.id, parser_provider="mineru_api")
    db.add(run)
    db.flush()
    blocks = [DocumentBlock(id=uuid4(), document_id=document.id, parse_run_id=run.id,
        block_index=i, block_type="text", text=f"synthetic {i}") for i in range(2)]
    db.add_all(blocks)
    db.commit()
    drafts = [replace(pdf_draft(content=f"synthetic {i}"), chunk_index=i, parse_run_id=run.id,
        block_refs=(ChunkBlockRef(block_id=block.id, block_order=0),)) for i, block in enumerate(blocks)]
    return drafts, run


def test_shared_writer_preserves_null_run_and_pdf_fixture_metadata(db, document):
    drafts = [pdf_draft()]
    before = deepcopy(asdict(drafts[0]))
    commits = db.commits
    chunk, = write(db, document.id, drafts)
    assert db.commits == commits
    assert chunk.parse_run_id is None and chunk.block_mappings == []
    assert count(db, DocumentParseRun) == count(db, DocumentBlock) == count(db, DocumentChunkBlock) == 0
    assert chunk.embedding is None and chunk.embedding_model is None and chunk.embedding_dim is None
    assert chunk.embedding_status == "not_started"
    db.commit()
    db.expire_all()
    stored = db.get(DocumentChunk, chunk.id)
    assert stored.source_metadata == before["source_metadata"]
    assert stored.source_metadata["kg_refs"][0]["clause_ref"] == "C0001"
    assert "table_ref" not in stored.source_metadata["kg_refs"][0]
    assert isinstance(stored.source_metadata["kg_refs"], list)
    assert stored.source_metadata["kg_refs"][1]["table_ref"] == "T0001"
    assert stored.chunk_method == "pdf_fixture" and stored.content_format == "markdown"
    assert document.process_status == "uploaded"  # Writer owns no document status.
    drafts[0].source_metadata["kg_refs"][0]["graph_id"] = "mutated"
    assert stored.source_metadata["kg_refs"][0]["graph_id"] == "G"


def test_shared_writer_preserves_order_content_metadata_and_links(db, document):
    drafts, run = linked_drafts(db, document)
    before = deepcopy([asdict(d) for d in drafts])
    actual = write(db, document.id, drafts)
    assert [asdict(d) for d in drafts] == before
    for chunk, draft in zip(actual, drafts, strict=True):
        assert chunk.parse_run_id == run.id
        for name in ("content", "token_count", "section_title", "chunk_type", "chunk_method", "content_format", "source_metadata"):
            assert getattr(chunk, name) == getattr(draft, name)
        assert [(m.block_id, m.block_order) for m in chunk.block_mappings] == [(r.block_id, r.block_order) for r in draft.block_refs]
    drafts[0].source_metadata["kg_refs"].clear()
    assert actual[0].source_metadata["kg_refs"]


@pytest.mark.parametrize("invalid", ["missing_block", "wrong_document", "wrong_run", "null_run", "duplicate_order"])
def test_writer_rejects_invalid_block_provenance_before_adding_chunks(db, document, invalid):
    drafts, _run = linked_drafts(db, document)
    first = drafts[0]
    ref = first.block_refs[0]
    if invalid == "missing_block":
        first = replace(first, block_refs=(replace(ref, block_id=uuid4()),))
    elif invalid == "null_run":
        first = replace(first, parse_run_id=None)
    elif invalid == "duplicate_order":
        first = replace(first, block_refs=(ref, ref))
    else:
        other = Document(id=uuid4(), original_filename="other.pdf", object_key="other.pdf", process_status="uploaded")
        db.add(other)
        db.flush()
        run = DocumentParseRun(id=uuid4(), document_id=other.id if invalid == "wrong_document" else document.id, parser_provider="mineru_api")
        db.add(run)
        db.flush()
        block = DocumentBlock(id=uuid4(), document_id=run.document_id, parse_run_id=run.id, block_index=0, block_type="text")
        db.add(block)
        db.commit()
        first = replace(first, block_refs=(replace(ref, block_id=block.id),))
    with pytest.raises(ValueError):
        write(db, document.id, [first, *drafts[1:]])
    assert count(db, DocumentChunk) == 0
    assert not any(isinstance(item, DocumentChunk) for item in db.new)


@pytest.mark.parametrize("failure_stage", ["chunks", "mappings"])
def test_writer_partial_flush_is_rollbackable_and_never_commits(db, document, failure_stage):
    drafts, _run = linked_drafts(db, document)
    commits = db.commits
    observed = []

    def fail_after_flush(session, context):
        target = DocumentChunk if failure_stage == "chunks" else DocumentChunkBlock
        if any(isinstance(item, target) for item in session.new):
            observed.append(session.connection().scalar(select(func.count()).select_from(target)))
            raise RuntimeError("injected partial write failure")

    event.listen(db, "after_flush", fail_after_flush)
    try:
        with pytest.raises(RuntimeError, match="partial write"):
            write(db, document.id, drafts)
    finally:
        event.remove(db, "after_flush", fail_after_flush)
        db.rollback()
    assert observed and observed[0] > 0
    assert db.commits == commits
    assert count(db, DocumentChunk) == count(db, DocumentChunkBlock) == 0


def test_empty_writer_does_not_flush_or_commit(db, document, monkeypatch):
    document_id = document.id
    monkeypatch.setattr(db, "flush", lambda *args, **kwargs: pytest.fail("empty write flushed"))
    assert write(db, document_id, []) == []
