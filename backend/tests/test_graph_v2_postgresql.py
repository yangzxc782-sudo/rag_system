"""M4 gated PostgreSQL/Checkpoint acceptance. COLLECT ONLY until separately authorized.

Creates a retained isolated test database at migration 0013, uses synthetic model,
embedding and index doubles, and never touches application DATABASE_URL or Neo4j.
"""
import os
from dataclasses import fields, asdict
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.services import rag
from app.services.hybrid_search import HybridSearchItem, HybridSearchResult
from app.services.graph_retrieval import GraphRetrievalService
from app.services.graph_sources import GraphSourceAuthority
from app.services.conversation_history import published_answer
from phase13_support import verified_engine
from phase13_m2_support import database_engine, factory, session, turn, invoke, call
from phase13_m3_support import runtime, result, publish_staged, redact, ANSWER
from test_kg_v2_builds import settings
from test_chunk_set_builds import prepare, finish
from test_graph_sources_v2 import setup
from test_rag_graph_fusion import graph_service
from app.ingestion.sequential_chunker import SegmentationConfig

pytestmark=pytest.mark.phase13_integration


@pytest.fixture(scope="module")
def m4_engine():
    if os.environ.get("PDF_KG_M4_TEST_MIGRATIONS") != "0013_pdf_kg_versions":
        pytest.skip("M4 PostgreSQL migrations and synthetic writes require separate authorization")
    root=verified_engine(os.environ.get("PHASE13_TEST_DATABASE_URL",""),os.environ.get("PHASE13_TEST_CLUSTER",""),
        os.environ.get("PHASE13_TEST_CONFIRMED_DATABASE",""))
    engine=database_engine(root,revision="0013_pdf_kg_versions")
    try: yield engine
    finally: engine.dispose(); root.dispose()


def test_persistent_v2_table_clause_replay_rechunk_and_source_redaction(m4_engine,settings,monkeypatch):
    with Session(m4_engine,autoflush=False) as db:
        values,built,context,_,encoder,index=setup(db,settings,SegmentationConfig(chunk_size=32,overlap=4))
        # Stub external retrieval at its established seam; SQL admission and snapshot
        # ownership/fingerprints/recovery use the actual migrated PostgreSQL records.
        keys={f.name for f in fields(HybridSearchItem)}
        candidates=[]
        for c in context.chunks:
            data={k:v for k,v in asdict(c).items() if k in keys}
            data.update(keyword_rank=1,vector_rank=1,matched_keywords=[],embedding_model="synthetic",embedding_dim=1024)
            candidates.append(HybridSearchItem(**data))
        def retrieve(_db,query,limit,document_id,_settings,**kwargs):
            search=HybridSearchResult(query,limit,len(candidates),candidates)
            return rag.RagRetrievalStage(rag.RagRerankOutcome(search,False,"disabled"),limit,None,len(candidates),0,0)
        retrieval=Mock(side_effect=retrieve)
        monkeypatch.setattr(rag,"retrieve_and_rerank",retrieval)
        online,repo=graph_service(settings)
        online.authority=GraphSourceAuthority(settings,factory(m4_engine))
        graph,pool,provider=runtime(m4_engine,graph_retrieval=online,graph_retrieval_enabled=True,
            pdf_kg_search_enabled=True,rag_graph_context_max_chars=20000,conversation_answer_graph_tokens=8192,
            conversation_answer_max_input_tokens=20000)
        try:
            current=turn(m4_engine,session(m4_engine),"冒口有什么作用？")
            state=invoke(graph,current)
            assert state["outcome"]=="answer"
            staged=result(graph,current)
            assert {e.ref.anchor_type for e in staged.graph_context.evidence}=={"table","clause"}
            calls=repo.fetch_anchor_context.call_count
            finish(db,settings,values,prepare(db,settings,values,SegmentationConfig(chunk_size=19,overlap=2),True),encoder,index)
            assert result(graph,current).graph_context==staged.graph_context
            assert invoke(graph,current)==state and repo.fetch_anchor_context.call_count==calls
            assert retrieval.call_count==len(provider.answer_calls)==1
            publish_staged(m4_engine,graph,current)
            # This is the existing isolated evidence-redaction helper, not a document-delete API.
            redact(m4_engine,values[0])
            history=call(m4_engine,lambda r:published_answer(r,r.get_turn(current.session_id,current.id)))
            assert history.answer==ANSWER and not history.graph.evidence
            assert any(s.kind=="graph" and s.status=="source_deleted" for s in history.sources)
        finally:
            pool.close()
