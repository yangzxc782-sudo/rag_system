"""M2 real SQL service contract, gated independently; not run in offline acceptance.

Retains a dedicated synthetic schema; never uses application DATABASE_URL.
Model/Neo4j/storage doubles here still are NOT real external-service acceptance.
"""
import os

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentProcessingJob, GraphBuild, KGExtractionUnit
from app.services.document_graph_builds import load_anchor_index
from phase13_support import schema_engine, verified_engine
from test_kg_v2_builds import settings, prepared, advance, Provider, Writer

pytestmark = pytest.mark.phase13_integration


@pytest.fixture(scope="module")
def m2_engine():
    if os.environ.get("PDF_KG_M2_TEST_MIGRATIONS") != "0013_pdf_kg_versions":
        pytest.skip("M2 requires separate authorization of dedicated PostgreSQL service tests")
    root = verified_engine(os.environ.get("PHASE13_TEST_DATABASE_URL", ""),
                           os.environ.get("PHASE13_TEST_CLUSTER", ""),
                           os.environ.get("PHASE13_TEST_CONFIRMED_DATABASE", ""))
    engine = schema_engine(root, revision="0013_pdf_kg_versions")
    try:
        yield engine
    finally:
        engine.dispose()
        root.dispose()


def test_graph_stage_against_actual_m0_constraints_and_triggers(m2_engine, settings):
    with Session(m2_engine, autoflush=False) as db:
        doc, version, build, assets, _ = prepared(db, settings, filename="m2-pg-stage.pdf")
        provider, writer = Provider(db), Writer(db)
        advance(db, settings, doc, build, assets, provider, writer)
        assert advance(db, settings, doc, build, assets, provider, writer)["status"] == "ready"
        assert len(load_anchor_index(db, doc, version, build)) == 1
        assert db.get(GraphBuild, build).sealed_at is not None
        assert db.scalar(select(KGExtractionUnit).where(KGExtractionUnit.graph_build_id == build)).has_qualified_triples
        assert db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.graph_build_id == build)).stage == "kg_ready"
