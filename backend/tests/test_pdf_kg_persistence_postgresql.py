"""Real PostgreSQL M0 contracts; NOT enabled by ordinary integration settings.

Requires separate authorization, PDF_KG_M0_TEST_MIGRATIONS=0013_pdf_kg_versions,
and the existing confirmed dedicated Phase 13 database/cluster variables.
Creates retained synthetic schemas, never uses application Settings or .env.
"""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import os
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    ChunkSet, Document, DocumentChunk, DocumentParseRun, DocumentProcessingJob,
    GraphBuild, KGExtractionUnit, SourceDocumentVersion,
)
from phase13_support import migration, schema_engine, verified_engine

pytestmark = pytest.mark.phase13_integration
REVISION = "0013_pdf_kg_versions"
HASH = "a" * 64
NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def m0_root():
    if os.environ.get("PDF_KG_M0_TEST_MIGRATIONS") != REVISION:
        pytest.skip("M0 real migration tests require separate explicit isolated-test authorization")
    root = verified_engine(
        os.environ.get("PHASE13_TEST_DATABASE_URL", ""),
        os.environ.get("PHASE13_TEST_CLUSTER", ""),
        os.environ.get("PHASE13_TEST_CONFIRMED_DATABASE", ""),
    )
    try:
        with root.connect() as db:
            assert db.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")), (
                "Provision vector in the authorized dedicated instance before this suite"
            )
        yield root
    finally:
        root.dispose()


@pytest.fixture(scope="module")
def m0_engine(m0_root):
    engine = schema_engine(m0_root, revision=REVISION)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db(m0_engine):
    with m0_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@contextmanager
def rejected(db, *, code="23514", message=None):
    with pytest.raises(IntegrityError) as caught:
        with db.begin_nested():
            yield
    assert caught.value.orig.sqlstate == code
    if message:
        assert message in str(caught.value.orig)


def put(db, model, **values):
    db.execute(model.__table__.insert().values(**values))


def change(db, model, identity, **values):
    pk = model.__table__.primary_key.columns.values()[0]
    db.execute(model.__table__.update().where(pk == identity).values(**values))


def legacy(db, *, file_type="pdf"):
    did, pid, cid = uuid4(), uuid4(), uuid4()
    put(db, Document, id=did, original_filename=f"历史.{file_type}", object_key=f"legacy/{did}", file_type=file_type)
    put(db, DocumentParseRun, id=pid, document_id=did, parser_provider="mineru", status="completed")
    put(db, DocumentChunk, id=cid, document_id=did, parse_run_id=pid, chunk_index=0,
        content="历史正文😀", source_metadata={"legacy": True})
    return did, pid, cid


def source(db, did, pid, **overrides):
    identity = uuid4()
    values = dict(source_version=identity, document_id=did, parse_run_id=pid, bucket_name="test-only",
                  canonical_object_key=f"source/{identity}/canonical.md", canonical_sha256=HASH, character_count=100,
                  block_map_object_key=f"source/{identity}/blocks.json", block_map_sha256=HASH,
                  cleaner_version="test-v1", renderer_version="test-v1", cleaning_config_sha256=HASH)
    values.update(overrides)
    put(db, SourceDocumentVersion, **values)
    return identity


def build(db, did, sid, **overrides):
    identity = uuid4()
    values = dict(id=identity, document_id=did, source_version=sid, graph_id=f"graph-{identity}",
                  identity_day=date(2026, 10, 6), source_path=f"documents/{did}/sources/{sid}/graphs/{identity}",
                  template_version="test-v2", template_sha256=HASH, unit_rule_version="test-v1",
                  unit_rule_sha256=HASH, provider_fingerprint=HASH, input_fingerprint=HASH)
    values.update(overrides)
    put(db, GraphBuild, **values)
    return identity


def chunk_set(db, did, sid, bid, **overrides):
    identity = uuid4()
    values = dict(id=identity, document_id=did, source_version=sid, graph_build_id=bid,
                  segmentation_version="sequential-v1", segmentation_config={"size": 20, "overlap": 2},
                  segmentation_config_sha256=HASH, embedding_fingerprint=HASH)
    values.update(overrides)
    put(db, ChunkSet, **values)
    return identity


def versions(db):
    did, pid, _ = legacy(db)
    sid = source(db, did, pid)
    bid = build(db, did, sid)
    csid = chunk_set(db, did, sid, bid)
    return did, pid, sid, bid, csid


def unit(db, did, sid, bid, **overrides):
    identity = uuid4()
    values = dict(id=identity, document_id=did, source_version=sid, graph_build_id=bid,
                  source_start=0, source_end=20, unit_index=0, kind="clause",
                  allocated_anchor_id=f"anchor-{identity}", allocated_anchor_metadata={"anchor_type": "clause"},
                  input_sha256=HASH)
    values.update(overrides)
    put(db, KGExtractionUnit, **values)
    return identity


def chunk(db, did, pid, sid, csid, **overrides):
    identity = uuid4()
    content = "甲😀乙"
    values = dict(id=identity, document_id=did, parse_run_id=pid, source_version=sid, chunk_set_id=csid,
                  source_start=0, source_end=3, content=content, content_sha256=sha256(content.encode()).hexdigest(),
                  chunk_index=0, source_metadata={"kg_refs": []})
    values.update(overrides)
    put(db, DocumentChunk, **values)
    return identity


def seal_build(db, bid, *, empty=True):
    change(db, GraphBuild, bid, status="ready_empty" if empty else "ready", sealed_at=NOW,
           result_object_key=f"graph/{bid}/result.json", result_sha256=HASH,
           unit_count=1, anchor_count=0 if empty else 1, entity_count=0 if empty else 2,
           relationship_count=0 if empty else 1)


def seal_set(db, csid):
    change(db, ChunkSet, csid, status="chunks_ready", sealed_at=NOW, chunk_count=1,
           manifest_object_key=f"chunks/{csid}/manifest.json", manifest_sha256=HASH)


def index_set(db, csid):
    change(db, ChunkSet, csid, status="indexed", indexed_at=NOW,
           index_name="synthetic-no-opensearch", index_receipt={"synthetic": True})


def test_real_upgrade_preserves_legacy_pdf_markdown_and_chunk_payloads(m0_root):
    engine = schema_engine(m0_root, revision="0012_casting_answers")
    try:
        # Current ORM column defaults omit the M0 columns when not supplied.
        with engine.begin() as connection:
            for kind in ("pdf", "md"):
                legacy(connection, file_type=kind)
            before_docs = connection.execute(text("SELECT to_jsonb(d) FROM documents d ORDER BY id")).scalars().all()
            before_chunks = connection.execute(text("SELECT to_jsonb(c) FROM document_chunks c ORDER BY id")).scalars().all()
            migration(connection, REVISION)
            after_docs = connection.execute(text("SELECT to_jsonb(d) - ARRAY['current_chunk_set_id','publication_revision'] FROM documents d ORDER BY id")).scalars().all()
            after_chunks = connection.execute(text("SELECT to_jsonb(c) - ARRAY['chunk_set_id','source_version','source_start','source_end','content_sha256'] FROM document_chunks c ORDER BY id")).scalars().all()
            assert before_docs == after_docs and before_chunks == after_chunks
            assert connection.scalar(text("SELECT count(*) FROM documents WHERE current_chunk_set_id IS NOT NULL OR publication_revision <> 0")) == 0
            for model in (SourceDocumentVersion, GraphBuild, KGExtractionUnit, ChunkSet, DocumentProcessingJob):
                assert connection.scalar(text(f"SELECT count(*) FROM {model.__tablename__}")) == 0
                assert {c["name"] for c in inspect(connection).get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
    finally:
        engine.dispose()


def test_cross_document_and_source_links_rejected(db):
    did, pid, sid, bid, csid = versions(db)
    other_doc, other_parse, _ = legacy(db)
    other_source = source(db, did, pid)
    for operation in (
        lambda: source(db, other_doc, pid),
        lambda: build(db, other_doc, sid),
        lambda: unit(db, did, other_source, bid),
        lambda: chunk_set(db, did, other_source, bid),
        lambda: chunk(db, did, other_parse, sid, csid),
        lambda: chunk(db, other_doc, other_parse, sid, csid),
    ):
        with rejected(db, code="23503"):
            operation()


@pytest.mark.parametrize("values", [
    {"source_start": -1}, {"source_end": 0}, {"source_end": 101}, {"source_end": 4},
    {"source_version": None}, {"chunk_set_id": None}, {"parse_run_id": None}, {"content_sha256": "invalid"},
])
def test_chunk_interval_contract_rejects_bad_or_partial_versions(db, values):
    did, pid, sid, bid, csid = versions(db)
    with rejected(db):
        chunk(db, did, pid, sid, csid, **values)


def test_unicode_length_overlap_and_legacy_duplicate_indices(db):
    did, pid, sid, bid, csid = versions(db)
    first = chunk(db, did, pid, sid, csid)
    chunk(db, did, pid, sid, csid, chunk_index=1, source_start=1, source_end=4)
    put(db, DocumentChunk, document_id=did, parse_run_id=pid, chunk_index=0, content="legacy duplicate")
    with rejected(db, code="23505"):
        chunk(db, did, pid, sid, csid)
    assert db.scalar(select(DocumentChunk.__table__.c.content).where(DocumentChunk.id == first)) == "甲😀乙"
    # Bounds/length are checked here; canonical equality is a later writer responsibility.


def test_graph_identity_is_unique_and_sources_are_immutable(db):
    did, pid, sid, bid, _ = versions(db)
    row = db.execute(select(GraphBuild.__table__).where(GraphBuild.id == bid)).mappings().one()
    for field in ("graph_id", "source_path"):
        with rejected(db, code="23505"):
            build(db, did, sid, **{field: row[field]})
    with rejected(db, message="SOURCE_IMMUTABLE"):
        change(db, SourceDocumentVersion, sid, character_count=200)
    with rejected(db, message="BUILD_IDENTITY"):
        change(db, GraphBuild, bid, graph_id="replacement")
    with rejected(db, message="SOURCE_DELETE"):
        db.execute(SourceDocumentVersion.__table__.delete().where(SourceDocumentVersion.source_version == sid))


def test_unit_empty_failure_and_sealed_results_are_distinct(db):
    did, pid, sid, bid, _ = versions(db)
    uid = unit(db, did, sid, bid)
    with rejected(db):
        unit(db, did, sid, bid, unit_index=1, source_end=101)
    with rejected(db):
        change(db, KGExtractionUnit, uid, status="succeeded_nonempty")
    change(db, KGExtractionUnit, uid, status="failed", last_error_code="SYNTHETIC_FAILURE")
    with rejected(db):
        change(db, GraphBuild, bid, status="ready_empty")
    change(db, KGExtractionUnit, uid, status="succeeded_empty", completed_at=NOW,
           result_object_key="synthetic/unit.json", result_sha256=HASH, last_error_code=None)
    with rejected(db, message="UNIT_IDENTITY_OR_RESULT"):
        change(db, KGExtractionUnit, uid, source_end=19)
    with rejected(db, message="SUCCESSFUL_UNIT_DELETE"):
        db.execute(KGExtractionUnit.__table__.delete().where(KGExtractionUnit.id == uid))
    seal_build(db, bid)
    with rejected(db, message="SEALED_BUILD_UNITS"):
        unit(db, did, sid, bid, unit_index=1)
    with rejected(db, message="BUILD_IDENTITY_OR_RESULT"):
        change(db, GraphBuild, bid, status="writing", sealed_at=None)


def test_chunk_seal_allows_embedding_until_indexed_but_no_content_rewrite(db):
    did, pid, sid, bid, csid = versions(db)
    cid = chunk(db, did, pid, sid, csid)
    seal_set(db, csid)
    with rejected(db, message="SEALED_SET_INSERT"):
        chunk(db, did, pid, sid, csid, chunk_index=1)
    with rejected(db, message="SEALED_CHUNK_CONTENT"):
        change(db, DocumentChunk, cid, source_metadata={"kg_refs": [{"fake": True}]})
    with rejected(db, message="SEALED_CHUNK_DELETE"):
        db.execute(DocumentChunk.__table__.delete().where(DocumentChunk.id == cid))
    change(db, DocumentChunk, cid, embedding_status="completed", embedding_model="synthetic", embedding_dim=1024)
    index_set(db, csid)
    with rejected(db, message="SEALED_CHUNK_CONTENT"):
        change(db, DocumentChunk, cid, embedding_model="changed")


def test_publication_is_ready_scoped_and_revisioned_and_keeps_history(db):
    did, pid, sid, bid, csid = versions(db)
    first = chunk(db, did, pid, sid, csid)
    with rejected(db, message="PUBLICATION_NOT_READY"):
        change(db, Document, did, current_chunk_set_id=csid, publication_revision=1)
    seal_build(db, bid)
    seal_set(db, csid)
    index_set(db, csid)
    with rejected(db, message="REVISION_MUST_ADVANCE"):
        change(db, Document, did, current_chunk_set_id=csid)
    change(db, Document, did, current_chunk_set_id=csid, publication_revision=1)
    other_doc, _, _ = legacy(db)
    with rejected(db, message="PUBLICATION_NOT_READY"):
        change(db, Document, other_doc, current_chunk_set_id=csid, publication_revision=1)
    next_set = chunk_set(db, did, sid, bid)
    chunk(db, did, pid, sid, next_set)
    seal_set(db, next_set)
    index_set(db, next_set)
    change(db, Document, did, current_chunk_set_id=next_set, publication_revision=2)
    assert db.scalar(select(DocumentChunk.id).where(DocumentChunk.id == first)) == first
    with rejected(db, message="REVISION_WITHOUT_SWITCH"):
        change(db, Document, did, publication_revision=3)
    change(db, Document, did, deletion_status="deleting")
    with rejected(db, message="PUBLICATION_NOT_READY"):
        change(db, Document, did, current_chunk_set_id=csid, publication_revision=3)
    change(db, Document, did, current_chunk_set_id=None, publication_revision=3)


def test_request_idempotency_active_job_and_fencing_contracts(db):
    did, _, sid, bid, csid = versions(db)
    jid, rid = uuid4(), uuid4()
    payload = dict(document_id=did, operation="rechunk", request_id=rid, input_fingerprint=HASH,
                   source_version=sid, graph_build_id=bid, stage="kg_ready")
    put(db, DocumentProcessingJob, id=jid, **payload)
    with rejected(db, code="23505"):
        put(db, DocumentProcessingJob, id=uuid4(), **payload)
    with rejected(db, code="23505"):
        put(db, DocumentProcessingJob, id=uuid4(), **{**payload, "request_id": uuid4()})
    with rejected(db, message="JOB_IDENTITY"):
        change(db, DocumentProcessingJob, jid, input_fingerprint="b" * 64)
    with rejected(db):
        change(db, DocumentProcessingJob, jid, status="running")
    token = uuid4()
    claim = dict(status="running", locked_by="test-worker", lease_token=token, locked_at=NOW,
                 lease_expires_at=NOW + timedelta(minutes=1), fencing_token=1, attempt_count=1)
    change(db, DocumentProcessingJob, jid, **claim)
    change(db, DocumentProcessingJob, jid, lease_expires_at=NOW + timedelta(minutes=2))
    with rejected(db, message="RENEWAL_IDENTITY_CHANGED"):
        change(db, DocumentProcessingJob, jid, locked_by="different-worker")
    with rejected(db, message="NEW_LEASE_REQUIRES_FENCE"):
        change(db, DocumentProcessingJob, jid, lease_token=uuid4())
    with rejected(db, message="COUNTER_REGRESSION"):
        change(db, DocumentProcessingJob, jid, fencing_token=0)
    change(db, DocumentProcessingJob, jid, status="retry_wait", locked_by=None, lease_token=None,
           locked_at=None, lease_expires_at=None, next_retry_at=NOW, last_error_code="SYNTHETIC_RETRY")
    change(db, DocumentProcessingJob, jid, **{**claim, "lease_token": uuid4(), "fencing_token": 2, "attempt_count": 2},
           next_retry_at=None, last_error_code=None)
    # No worker claims, writes or external jobs are implemented by these table tests.


def test_new_records_block_legacy_hard_delete_instead_of_cascading(db):
    did, pid, sid, bid, csid = versions(db)
    with rejected(db, code="23503"):
        db.execute(Document.__table__.delete().where(Document.id == did))
    with rejected(db, code="23503"):
        db.execute(DocumentParseRun.__table__.delete().where(DocumentParseRun.id == pid))
