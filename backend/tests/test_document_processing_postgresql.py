"""Separately authorized M5 PostgreSQL acceptance; never run in offline suite.

Creates/retains a dedicated schema via existing verified test-cluster gates.
Model, PDF transport, MinIO, Neo4j and index gateways remain synthetic here.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import Document, DocumentProcessingJob
from app.services.document_processing import request_processing
from app.ingestion.sequential_chunker import SegmentationConfig
from phase13_support import schema_engine, verified_engine
from test_document_processing import settings, parsed, finish
from test_document_version_deletion import test_version_manifest_revokes_publication_and_finalizes_dependencies as run_deletion_contract
from test_kg_v2_builds import Provider, Writer
from test_chunk_set_builds import Encoder, Index

pytestmark = pytest.mark.phase13_integration


@pytest.fixture(scope="module")
def m5_engine():
    if os.environ.get("PDF_KG_M5_TEST_MIGRATIONS") != "0013_pdf_kg_versions":
        pytest.skip("Requires separate authorization for M5 isolated PostgreSQL migrations/tests")
    root = verified_engine(os.environ.get("PHASE13_TEST_DATABASE_URL", ""), os.environ.get("PHASE13_TEST_CLUSTER", ""),
        os.environ.get("PHASE13_TEST_CONFIRMED_DATABASE", ""))
    engine = schema_engine(root, revision="0013_pdf_kg_versions")
    try:
        yield engine
    finally:
        engine.dispose()
        root.dispose()


def test_complete_pipeline_on_actual_m0_triggers(m5_engine, settings, monkeypatch):
    with Session(m5_engine, autoflush=False) as db:
        doc, job, assets, _ = parsed(db, settings, monkeypatch)
        state, _ = finish(db, settings, doc, job, assets, graph_provider=Provider(db), writer=Writer(db),
            encoder=Encoder(db, settings), index=Index(db))
        assert state["status"] == "succeeded"
        assert db.get(Document, doc).current_chunk_set_id == state["chunk_set_id"]
        saved = db.get(DocumentProcessingJob, job)
        request = saved.request_id
        db.commit()
        again = request_processing(db, doc, request, SegmentationConfig(chunk_size=50, overlap=5), settings=settings)
        assert again["job_id"] == job


def test_version_deletion_on_actual_m0_triggers(m5_engine, settings, monkeypatch):
    # A separate retained schema keeps the whole-database absence assertions
    # isolated from the preceding pipeline test's preserved document.
    root = verified_engine(os.environ.get("PHASE13_TEST_DATABASE_URL", ""), os.environ.get("PHASE13_TEST_CLUSTER", ""),
        os.environ.get("PHASE13_TEST_CONFIRMED_DATABASE", ""))
    isolated = schema_engine(root, revision="0013_pdf_kg_versions")
    try:
        with Session(isolated, autoflush=False) as db:
            run_deletion_contract(db, settings, monkeypatch)
    finally:
        isolated.dispose()
        root.dispose()


def test_concurrent_duplicate_requests_and_claim_exclusion(m5_engine, settings):
    from app.core.errors import BusinessError
    from app.services.document_processing import advance_processing
    from app.services.document_deletion import request_document_deletion
    settings.document_processing_executor_enabled = settings.pdf_kg_chunks_enabled = True
    settings.document_deletion_executor_enabled = True
    settings.pdf_kg_embedding_revision = "synthetic-m5-concurrency"
    document_id, request_id = uuid4(), uuid4()
    with Session(m5_engine) as db:
        db.add(Document(id=document_id, original_filename="claim.pdf", file_type=".pdf", bucket_name="synthetic",
            object_key=f"raw/2026/10/{document_id}.pdf", process_status="uploaded"))
        db.commit()
    barrier = Barrier(2)
    def submit():
        with Session(m5_engine, autoflush=False) as db:
            barrier.wait(timeout=10)
            return request_processing(db, document_id, request_id, SegmentationConfig(), settings=settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.submit(submit), pool.submit(submit)
        first, second = one.result(timeout=15), two.result(timeout=15)
    assert first["job_id"] == second["job_id"]
    entered, release = Event(), Event()
    def blocking_parser(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        raise RuntimeError("synthetic provider failure")
    def run():
        with Session(m5_engine, autoflush=False) as db:
            with pytest.raises(RuntimeError):
                advance_processing(db, document_id, first["job_id"], settings=settings,
                    worker_id="first", parser=blocking_parser)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            assert entered.wait(10)
            with Session(m5_engine, autoflush=False) as db:
                assert not advance_processing(db, document_id, first["job_id"], settings=settings,
                    worker_id="second", parser=lambda *a, **k: pytest.fail("duplicate parse"))
                with pytest.raises(BusinessError) as exc:
                    request_document_deletion(db, document_id, settings=settings)
                assert exc.value.code == "DOCUMENT_PROCESSING_IN_PROGRESS"
        finally:
            release.set()
        future.result(timeout=15)
