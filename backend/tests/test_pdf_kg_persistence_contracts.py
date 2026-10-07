"""Offline M0 checks. SQLite below tests legacy ORM SQL only, never PG guards."""
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
import re
from uuid import uuid4

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, configure_mappers
from sqlalchemy.schema import AddConstraint, CreateColumn, CreateIndex, CreateTable

from app.db.base import Base
from app.models import (
    ChunkSet, Document, DocumentChunk, DocumentProcessingJob, GraphBuild,
    KGExtractionUnit, SourceDocumentVersion,
)

BACKEND = Path(__file__).resolve().parents[1]
NEW_MODELS = (SourceDocumentVersion, GraphBuild, KGExtractionUnit, ChunkSet, DocumentProcessingJob)
DOCUMENT_ADDITIONS = {"current_chunk_set_id", "publication_revision"}
CHUNK_ADDITIONS = {"chunk_set_id", "source_version", "source_start", "source_end", "content_sha256"}
ADDED_CONSTRAINTS = {
    "documents": {"fk_documents_current_chunk_set", "ck_documents_publication_revision"},
    "document_parse_runs": {"uq_document_parse_runs_owner"},
    "document_chunks": {
        "uq_document_chunks_set_index", "fk_document_chunks_set_owner",
        "fk_document_chunks_source_parse", "ck_document_chunks_source_contract",
    },
}


def migration_module():
    spec = spec_from_file_location("pdf_kg_migration", BACKEND / "alembic/versions/0013_pdf_kg_versions.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalized(sql):
    return " ".join(str(sql).split()).rstrip(";")


def compiled(value):
    return str(value.compile(dialect=postgresql.dialect()))


def test_additive_migration_has_one_head_and_no_data_rewrite():
    module = migration_module()
    config = Config()
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == [module.revision]
    assert module.down_revision == "0012_casting_answers"
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        module.upgrade()
    sql = output.getvalue()
    assert set(re.findall(r"CREATE TABLE (\w+)", sql)) == {m.__tablename__ for m in NEW_MODELS}
    # Trigger event names contain UPDATE/DELETE; actual data statements must not.
    assert not re.search(r"\b(DROP|TRUNCATE)\b|\bDELETE\s+FROM\b|\bINSERT\s+INTO\b|\bUPDATE\s+\w+\s+SET\b", sql)
    assert "ON DELETE CASCADE" not in sql
    with pytest.raises(RuntimeError, match="retain"):
        module.downgrade()


@pytest.mark.parametrize("model", NEW_MODELS)
def test_frozen_migration_matches_model_columns_constraints_and_indexes(model):
    # Compare complete definitions, not selected strings; drift requires a deliberate migration edit.
    table = model.__table__
    ddl = migration_module().TABLE_DDL
    actual = re.search(rf"CREATE TABLE {table.name} \((.*?)\n\);", ddl, re.S).group(1)
    expected = compiled(CreateTable(table)).split("(", 1)[1].rsplit(")", 1)[0]
    lines = lambda body: {normalized(line.rstrip().rstrip(",")) for line in body.splitlines() if line.strip()}
    assert lines(actual) == lines(expected)
    for index in table.indexes:
        assert normalized(compiled(CreateIndex(index))) in normalized(ddl)


def test_legacy_table_additions_match_migration_and_keep_reference_constraints():
    ddl = normalized(migration_module().TABLE_DDL)
    for model, names in ((Document, DOCUMENT_ADDITIONS), (DocumentChunk, CHUNK_ADDITIONS)):
        for name in names:
            column = model.__table__.c[name]
            assert normalized(f"ALTER TABLE {model.__tablename__} ADD COLUMN {compiled(CreateColumn(column))}") in ddl
    for name, constraints in ADDED_CONSTRAINTS.items():
        for constraint in Base.metadata.tables[name].constraints:
            if constraint.name in constraints:
                assert normalized(compiled(AddConstraint(constraint, isolate_from_table=False))) in ddl
    foreign_keys = Base.metadata.tables["knowledge_item_chunks"].c.chunk_id.foreign_keys
    assert any(f.target_fullname == "document_chunks.id" and f.ondelete is None for f in foreign_keys)
    # QA provenance deliberately has no chunk FK; do not retrofit historical snapshots.
    assert not Base.metadata.tables["qa_evidence_sources"].c.chunk_id.foreign_keys


def test_registered_models_have_scalar_intervals_and_extraction_is_chunk_independent():
    configure_mappers()
    for model in NEW_MODELS:
        assert Base.metadata.tables[model.__tablename__] is model.__table__
        assert not any(name.endswith("_spans") for name in model.__table__.columns.keys())
    for model in (KGExtractionUnit, DocumentChunk):
        assert {"source_version", "source_start", "source_end"} <= set(model.__table__.columns.keys())
    assert not ({"chunk_id", "chunk_index", "chunk_size", "overlap", "chunk_set_id"} & set(KGExtractionUnit.__table__.columns.keys()))
    assert all(DocumentChunk.__table__.c[name].nullable for name in CHUNK_ADDITIONS)
    assert not Document.__table__.c.publication_revision.nullable


def test_owner_foreign_keys_include_document_and_version():
    expected = {
        "fk_source_versions_parse_owner": ("parse_run_id", "document_id"),
        "fk_graph_builds_source_owner": ("source_version", "document_id"),
        "fk_kg_units_build_owner": ("graph_build_id", "document_id", "source_version"),
        "fk_chunk_sets_build_owner": ("graph_build_id", "document_id", "source_version"),
        "fk_document_chunks_set_owner": ("chunk_set_id", "document_id", "source_version"),
        "fk_document_chunks_source_parse": ("source_version", "document_id", "parse_run_id"),
        "fk_processing_jobs_set_owner": ("chunk_set_id", "document_id", "source_version", "graph_build_id"),
        "fk_documents_current_chunk_set": ("current_chunk_set_id", "id"),
    }
    actual = {fk.name: fk for table in Base.metadata.tables.values() for fk in table.foreign_key_constraints}
    for name, columns in expected.items():
        assert tuple(actual[name].column_keys) == columns
        assert actual[name].ondelete == "RESTRICT"


def test_legacy_selects_and_flushes_work_without_m0_columns():
    # Explicit pre-M0 schema fixture. No new tables, PostgreSQL or external IO.
    engine = create_engine("sqlite://")
    statements = []
    with engine.begin() as db:
        db.exec_driver_sql("""CREATE TABLE documents (
            id CHAR(32) PRIMARY KEY, original_filename TEXT NOT NULL, bucket_name TEXT,
            object_key TEXT NOT NULL, file_type TEXT, mime_type TEXT, file_size BIGINT,
            file_hash TEXT, process_status TEXT, deletion_status TEXT DEFAULT 'normal',
            error_message TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        db.exec_driver_sql("""CREATE TABLE document_chunks (
            id CHAR(32) PRIMARY KEY, document_id CHAR(32), parse_run_id CHAR(32),
            chunk_index INTEGER, content TEXT, token_count INTEGER, page_start INTEGER,
            page_end INTEGER, section_title TEXT, chunk_type TEXT, chunk_method TEXT,
            content_format TEXT, source_metadata JSON, embedding TEXT, embedding_model TEXT,
            embedding_dim INTEGER, embedding_status TEXT, embedding_error_message TEXT,
            embedding_updated_at DATETIME, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, sql, params, ctx, many: statements.append(sql))
    did, cid = uuid4(), uuid4()
    try:
        with Session(engine) as db:
            db.add(Document(id=did, original_filename="legacy.md", object_key="legacy.md", file_type="md"))
            db.add(DocumentChunk(id=cid, document_id=did, chunk_index=0, content="保留旧正文😀", source_metadata={"legacy": True}))
            db.commit()
        with Session(engine) as db:
            document, chunk = db.get(Document, did), db.get(DocumentChunk, cid)
            assert document.original_filename == "legacy.md" and chunk.content == "保留旧正文😀"
            assert chunk.source_metadata == {"legacy": True}
            assert DOCUMENT_ADDITIONS <= inspect(document).unloaded
            assert CHUNK_ADDITIONS <= inspect(chunk).unloaded
            db.refresh(chunk)
            assert chunk.content == "保留旧正文😀"
        for name in DOCUMENT_ADDITIONS | CHUNK_ADDITIONS:
            assert not any(re.search(rf"\b{name}\b", sql) for sql in statements)
        for model in (Document, DocumentChunk):
            assert not any(name in str(select(model)) for name in DOCUMENT_ADDITIONS | CHUNK_ADDITIONS)
    finally:
        engine.dispose()
