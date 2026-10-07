from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import pytest
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import CheckConstraint, JSON, DefaultClause, MetaData, create_engine, event, func, select, text
from sqlalchemy.dialects.postgresql import JSONB, dialect
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import Document, DocumentBlock, DocumentChunk, DocumentChunkBlock, DocumentParseRun
from app.ingestion.block_chunker import BlockChunkerConfig, build_block_aware_chunks
from app.ingestion.chunk_drafts import ChunkBlockRef
from pdf_source_fixtures import pdf_draft
from app.ingestion.mineru.normalizer import normalize_mineru_result
from app.services.document_blocks import add_document_blocks
from test_document_parsing_mineru import fake_parse_result


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


def adapt(result):
    from app.ingestion.chunk_adapters import adapt_mineru_chunks

    return adapt_mineru_chunks(result)


def mineru_result(db, document, size=120, overlap=0):
    run = DocumentParseRun(id=uuid4(), document_id=document.id, parser_provider="mineru_api")
    db.add(run)
    db.flush()
    normalized = normalize_mineru_result(fake_parse_result(), document_id=str(document.id),
                                        parse_run_id=str(run.id), output_prefix="parsed-assets")
    blocks = add_document_blocks(db, document_id=document.id, parse_run_id=run.id, blocks=normalized.blocks)
    db.commit()
    result = build_block_aware_chunks(blocks, parse_run_id=str(run.id), config=BlockChunkerConfig(
        max_chunk_chars=size, min_chunk_chars=0, overlap_chars=overlap))
    return result, run


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


def legacy_reference(result, document_id, run_id):
    """Frozen field projection of d9a3d16's two persistence helpers, no new adapter."""
    objects = [DocumentChunk(
        id=uuid4(), document_id=document_id, parse_run_id=run_id,
        chunk_index=item.chunk_index, content=item.content, token_count=item.estimated_token_count,
        page_start=item.page_start, page_end=item.page_end, section_title=item.section_title,
        chunk_type=item.chunk_type, chunk_method=item.chunk_method, content_format=item.content_format,
        source_metadata=dict(item.source_metadata), embedding=None, embedding_model=None,
        embedding_dim=None, embedding_status="not_started",
    ) for item in result.chunks]
    ids = {item.chunk_index: item.id for item in objects}
    from uuid import UUID
    mappings = [DocumentChunkBlock(id=uuid4(), chunk_id=ids[link.chunk_index],
                block_id=UUID(link.block_id), block_order=link.block_order) for link in result.links]
    return objects, mappings


def projection(chunks, mappings):
    indexes = {chunk.id: chunk.chunk_index for chunk in chunks}
    values = [{column.key: getattr(chunk, column.key) for column in DocumentChunk.__table__.columns
               if column.key not in {"id", "created_at", "updated_at"}} for chunk in chunks]
    links = [(indexes[mapping.chunk_id], mapping.block_id, mapping.block_order) for mapping in mappings]
    return values, links


@pytest.mark.parametrize("size,overlap", [(120, 0), (70, 20), (1000, 0)])
def test_mineru_adapter_writer_equals_pre_m3_persistence(db, document, size, overlap):
    result, run = mineru_result(db, document, size, overlap)
    before = deepcopy(asdict(result))
    expected = projection(*legacy_reference(result, document.id, run.id))
    drafts = adapt(result)
    actual = write(db, document.id, drafts)
    mappings = [
        mapping for chunk in actual for mapping in sorted(chunk.block_mappings, key=lambda item: item.block_order)]
    assert projection(actual, mappings) == expected
    assert all(item.parse_run_id == run.id for item in actual)
    assert asdict(result) == before
    assert all(isinstance(ref, ChunkBlockRef) for draft in drafts for ref in draft.block_refs)


@pytest.mark.parametrize("bad_link", ["missing", "invalid", "unknown_chunk"])
def test_adapter_rejects_dangling_links(db, document, bad_link):
    result, _run = mineru_result(db, document)
    link = result.links[0]
    link = replace(link, block_id=None if bad_link == "missing" else "not-a-uuid") if bad_link != "unknown_chunk" else replace(link, chunk_index=99)
    with pytest.raises(ValueError):
        adapt(replace(result, links=[link]))
    assert count(db, DocumentChunk) == 0


def test_adapter_copies_metadata_and_keeps_link_order(db, document):
    result, _run = mineru_result(db, document)
    drafts = adapt(result)
    assert [(draft.chunk_index, ref.block_order, str(ref.block_id)) for draft in drafts for ref in draft.block_refs] == [
        (link.chunk_index, link.block_order, link.block_id) for link in result.links]
    drafts[0].source_metadata["block_ids"].append("changed")
    assert "changed" not in result.chunks[0].source_metadata["block_ids"]


@pytest.mark.parametrize("invalid", ["missing_block", "wrong_document", "wrong_run", "null_run", "duplicate_order"])
def test_writer_rejects_invalid_block_provenance_before_adding_chunks(db, document, invalid):
    result, _run = mineru_result(db, document)
    drafts = adapt(result)
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
    result, _run = mineru_result(db, document)
    drafts = adapt(result)
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


def test_adapter_is_a_pure_domain_conversion():
    path = Path(__file__).resolve().parents[1] / "app/ingestion/chunk_adapters.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(name and name.startswith(("sqlalchemy", "app.models", "app.services")) for name in modules)
