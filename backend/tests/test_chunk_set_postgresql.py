"""Separately authorized PostgreSQL service acceptance; no real external providers.

Collect-only in M3. Dedicated retained schema, never application DATABASE_URL.
"""
import os
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from phase13_support import schema_engine, verified_engine
from app.models import Document, DocumentProcessingJob
from app.services.document_chunk_sets import chunk_set_status
from test_kg_v2_builds import settings
from test_chunk_set_builds import ready, prepare, finish, Encoder, Index

pytestmark = pytest.mark.phase13_integration


@pytest.fixture(scope="module")
def m3_engine():
    if os.environ.get("PDF_KG_M3_TEST_MIGRATIONS") != "0013_pdf_kg_versions":
        pytest.skip("M3 dedicated PostgreSQL tests require separate authorization")
    root = verified_engine(os.environ.get("PHASE13_TEST_DATABASE_URL", ""), os.environ.get("PHASE13_TEST_CLUSTER", ""),
                           os.environ.get("PHASE13_TEST_CONFIRMED_DATABASE", ""))
    engine = schema_engine(root, revision="0013_pdf_kg_versions")
    try:
        yield engine
    finally:
        engine.dispose(); root.dispose()


def test_m3_service_with_actual_m0_fks_seals_and_publication_trigger(m3_engine, settings):
    with Session(m3_engine, autoflush=False) as db:
        values = ready(db, settings)
        encoder, index = Encoder(db, settings), Index(db)
        initial = finish(db, settings, values, prepare(db, settings, values), encoder, index)
        final = finish(db, settings, values, prepare(db, settings, values, rechunk=True), encoder, index)
        assert db.get(Document, values[0]).publication_revision == 2
        assert db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.chunk_set_id == final["chunk_set_id"])).status == "succeeded"
        db.commit()
        assert chunk_set_status(db, values[0], initial["chunk_set_id"])["status"] == "indexed"
