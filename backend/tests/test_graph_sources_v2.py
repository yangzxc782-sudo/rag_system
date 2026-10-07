"""Offline SQLite authority + real M4 context/Neo4j decoder. No external services."""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import DocumentChunk, SourceDocumentVersion, KGExtractionUnit
from app.ingestion.sequential_chunker import SegmentationConfig
from app.rag.context_builder import build_rag_context, TRUNCATION_MARKER, format_context_for_prompt
from app.rag.graph_context_builder import build_graph_context, format_graph_context_for_prompt
from app.services.graph_sources import GraphSourceAuthority, validate_evidence_bindings
from app.services.graph_retrieval import GraphRetrievalService
from app.services import rag
from app.services.hybrid_search import HybridSearchItem, HybridSearchResult
from app.schemas.conversation_rag import CitationPayload, GraphPayload, EvidenceDetails, read_graph_payload
from app.rag.conversation_nodes import ConversationRagNodes, source_refs, graph_refs
from app.rag.conversation_prompt import checked_request, prompt_fingerprint
from app.rag.history_budget import HistoryContext
from app.services.conversation_repository import ConversationError
from test_chunk_set_builds import db, settings, ready, prepare, finish, Encoder, Index, service
from graph_v2_support import repository, FakeDriver, row, nested_properties, reorder_objects
from app.services.chat_evidence import ChatEvidenceService
from test_rag_service import FakeLLMProvider


def context_for(db, settings, chunk_set_id, budget=30000):
    chunks = service._chunks(db, chunk_set_id)
    parent = db.get(service.ChunkSet, chunk_set_id)
    items = [HybridSearchItem(chunk_id=str(c.id), document_id=str(c.document_id), original_filename="test.pdf",
        chunk_index=c.chunk_index, content=c.content, source_metadata=deepcopy(c.source_metadata),
        source_version=str(c.source_version), graph_build_id=str(parent.graph_build_id), chunk_set_id=str(c.chunk_set_id),
        source_start=c.source_start, source_end=c.source_end, content_sha256=c.content_sha256,
        embedding_fingerprint=parent.embedding_fingerprint, retrieval_source="both", hybrid_score=1/(c.chunk_index+1),
        keyword_score=1, vector_score=1, keyword_rank=1, vector_rank=1, matched_keywords=[],
        embedding_model=c.embedding_model, embedding_dim=c.embedding_dim) for c in chunks]
    db.commit()
    return build_rag_context("q", HybridSearchResult("q",len(items),len(items),items),
        SimpleNamespace(rag_context_max_chars=budget), preserve_order=True)


def setup(db, settings, config=None, empty=False):
    settings.graph_retrieval_enabled = settings.pdf_kg_search_enabled = True
    settings.rag_graph_context_max_chars = 20000
    values = ready(db, settings, empty=empty)
    encoder,index=Encoder(db,settings),Index(db)
    built=finish(db,settings,values,prepare(db,settings,values,config or SegmentationConfig(chunk_size=1000,overlap=0)),encoder,index)
    context=context_for(db,settings,built["chunk_set_id"])
    authority=GraphSourceAuthority(settings,sessionmaker(bind=db.get_bind(),autoflush=False))
    return values,built,context,authority,encoder,index


def graph_for(context, settings, authority):
    bindings=authority.resolve(context)
    repo,driver,_=repository(FakeDriver([row(a) for a in bindings]))
    graph_service=GraphRetrievalService(settings,repository=repo,authority=authority)
    return rag.build_graph_context_for_rag(context,settings,graph_service),driver


def test_sql_table_clause_bindings_and_release_before_query(db,settings):
    values,built,context,authority,_,_=setup(db,settings)
    bindings=authority.resolve(context)
    assert {a.ref.anchor_type for a in bindings}=={"table","clause"}
    graph,driver=graph_for(context,settings,authority)
    assert len(graph.evidence)==2 and len(driver.queries)==1
    assert not db.in_transaction()
    assert validate_evidence_bindings(graph,bindings)
    assert all(a.fully_covered for a in bindings)
    assert "kg-anchor" not in format_context_for_prompt(context)


def test_prefix_end_removes_later_anchor_and_partial_has_no_facts(db,settings):
    _,_,context,authority,_,_=setup(db,settings)
    anchors=authority.resolve(context)
    later=max(anchors,key=lambda a:a.source.source_start)
    c=context.chunks[0]
    end=later.source.source_start
    cut=replace(c,content=c.content[:end-c.source_start]+TRUNCATION_MARKER,
        effective_end=end,was_truncated=True)
    partial=replace(context,chunks=[cut])
    assert later.ref not in [a.ref for a in authority.resolve(partial)]
    # Cutting inside the clause maps it but cannot admit facts.
    earlier=min(anchors,key=lambda a:a.source.source_start)
    end=earlier.source.source_start+1
    partial=replace(context,chunks=[replace(c,content=c.content[:end]+TRUNCATION_MARKER,effective_end=end,was_truncated=True)])
    graph,driver=graph_for(partial,settings,authority)
    assert driver.queries and not graph.evidence
    assert graph.diagnostics[0].mapped and graph.diagnostics[0].query_status=="success"
    assert graph.diagnostics[0].use_status=="incomplete_coverage"


@pytest.mark.parametrize("change", [
    {"source_version":str(UUID(int=999))},{"graph_build_id":str(UUID(int=999))},
    {"document_id":str(UUID(int=999))},{"chunk_set_id":str(UUID(int=999))},
    {"source_start":1},{"content_sha256":"f"*64},{"embedding_fingerprint":"f"*64},
    {"content":"forged"},{"effective_end":0},{"was_truncated":True},
    {"source_metadata":{"kg_refs":[]}}, {"chunk_set_id":None},
], ids=["source","build","document","set","start","hash","embedding","content","effective","truncated","refs","legacy"])
def test_forged_final_evidence_rejected_before_neo4j(db,settings,change):
    _,_,context,authority,_,_=setup(db,settings)
    corrupt=replace(context,chunks=[replace(context.chunks[0],**change)])
    repo=Mock()
    graph=rag.build_graph_context_for_rag(corrupt,settings,GraphRetrievalService(settings,repository=repo,authority=authority))
    assert graph.source_error=="source_invalid" and not graph.evidence
    repo.fetch_anchor_context.assert_not_called()


def test_three_sequential_sets_reuse_graph_and_old_reference_is_restorable(db,settings,monkeypatch):
    values,first,old,authority,encoder,index=setup(db,settings)
    old_bindings=authority.resolve(old)
    old_graph,_=graph_for(old,settings,authority)
    for name in ("prepare_graph_build","advance_graph_build","make_graph_id","extract_piece","configured_writer"):
        monkeypatch.setattr(__import__("app.services.document_graph_builds",fromlist=[name]),name,
            Mock(side_effect=AssertionError("M4/rechunk must not construct graph")))
    for config in [SegmentationConfig(chunk_size=17,overlap=0,boundary="characters"),
                   SegmentationConfig(chunk_size=42,overlap=9,boundary="line")]:
        newer=finish(db,settings,values,prepare(db,settings,values,config,True),encoder,index)
        current=context_for(db,settings,newer["chunk_set_id"])
        current_bindings=authority.resolve(current)
        assert {a.ref.anchor_id for a in current_bindings}=={a.ref.anchor_id for a in old_bindings}
        assert all(a.fully_covered for a in current_bindings)
        assert any(len(a.provenance)>1 for a in current_bindings)
        graph,_=graph_for(current,settings,authority)
        assert len(graph.evidence)==2
    with pytest.raises(ValueError): authority.resolve(old)
    assert validate_evidence_bindings(old_graph,authority.resolve(old,current=False))
    assert len(values[-2].calls)==2 and len(values[-1].calls)==1


def test_retained_chunks_with_gap_do_not_cover_unit(db,settings):
    _,_,context,authority,_,_=setup(db,settings,SegmentationConfig(chunk_size=10,overlap=0))
    anchors=authority.resolve(context)
    table=next(a for a in anchors if a.ref.anchor_type=="table")
    origins=sorted(table.provenance,key=lambda p:p.effective_start)
    assert len(origins)>=3
    removed=origins[len(origins)//2].chunk_id
    gapped=replace(context,chunks=[c for c in context.chunks if c.chunk_id!=removed])
    graph,_=graph_for(gapped,settings,authority)
    diagnostic=next(d for d in graph.diagnostics if d.anchor_id==table.ref.anchor_id)
    assert diagnostic.mapped and diagnostic.query_status=="success" and not diagnostic.facts_used


def test_ready_empty_has_no_queries(db,settings):
    _,_,context,authority,_,_=setup(db,settings,empty=True)
    repo=Mock()
    graph=rag.build_graph_context_for_rag(context,settings,GraphRetrievalService(settings,repository=repo,authority=authority))
    assert not graph.evidence and not graph.diagnostics and graph.source_error is None
    repo.fetch_anchor_context.assert_not_called()


def saved_evidence(db,settings,context,graph):
    sid,tid,aid=uuid4(),uuid4(),uuid4()
    identity=SimpleNamespace(thread_id=sid,turn_id=tid,attempt_no=1)
    turn=SimpleNamespace(question="q")
    views={}
    for i,c in enumerate(context.chunks):
        views["c"+str(i)]=SimpleNamespace(kind="citation",status="available",payload=CitationPayload(chunk=c).model_dump(mode="json"),sources=source_refs(c))
    for i,g in enumerate(graph.evidence):
        views["g"+str(i)]=SimpleNamespace(kind="graph",status="available",payload=GraphPayload(evidence=g).model_dump(mode="json"),sources=graph_refs(g))
    request=checked_request("q","q",HistoryContext(),context,graph,settings)
    details=EvidenceDetails(evidence_generation=1,citation_keys=[k for k in views if k.startswith("c")],
        graph_keys=[k for k in views if k.startswith("g")],history_message_ids=[],graph_enabled=True,graph_triggered=True,
        graph_truncated=False,graph_schema_version=2,graph_diagnostics=graph.diagnostics,
        input_estimated_tokens=1000,prompt_fingerprint=prompt_fingerprint(request.messages))
    repo=SimpleNamespace(db=db,get_snapshot_by_key=lambda _sid,_tid,_aid,key:views[key])
    nodes=object.__new__(ConversationRagNodes);nodes.settings=settings
    return nodes,repo,identity,turn,(SimpleNamespace(id=aid),details),views


def test_phase13_roundtrip_restore_after_rechunk_and_fingerprint_guard(db,settings,monkeypatch):
    values,first,context,authority,encoder,index=setup(db,settings)
    graph,_=graph_for(context,settings,authority)
    args=saved_evidence(db,settings,context,graph)
    nodes,repo,identity,turn,saved,views=args
    monkeypatch.setattr("app.rag.conversation_nodes.rehydrate_history",lambda *a:HistoryContext())
    for view in views.values():
        if view.kind=="graph":
            assert read_graph_payload(view.payload).ref.anchor_type in {"table","clause"}
    finish(db,settings,values,prepare(db,settings,values,SegmentationConfig(chunk_size=21,overlap=3),True),encoder,index)
    plan,_,request=nodes._evidence(repo,identity,turn,saved,"q")
    assert plan.graph.evidence==graph.evidence
    assert prompt_fingerprint(request.messages)==saved[1].prompt_fingerprint
    wrong=(saved[0],saved[1].model_copy(update={"prompt_fingerprint":"f"*64}))
    with pytest.raises(ConversationError) as error: nodes._evidence(repo,identity,turn,wrong,"q")
    assert error.value.code=="QA_PROMPT_CHANGED"
    # An older snapshot is preserved as data and explicitly rejected for execution.
    original=deepcopy(views["g0"].payload); views["g0"].payload.pop("schema_version")
    with pytest.raises(ConversationError) as error: nodes._evidence(repo,identity,turn,saved,"q")
    assert error.value.code=="QA_GRAPH_EVIDENCE_VERSION_UNSUPPORTED"
    views["g0"].payload=original
    # Changed effective text cannot inherit the old graph provenance.
    views["c0"].payload["chunk"]["effective_end"]-=1
    with pytest.raises(ConversationError) as error: nodes._evidence(repo,identity,turn,saved,"q")
    assert error.value.code=="QA_SOURCE_INVALID"


@pytest.mark.parametrize("corruption", [None, "value", "array_order", "fingerprint"])
def test_first_attempt_build_persist_reorder_and_generate_guard(db, settings, monkeypatch, corruption):
    """Real node/formatter/source checks; only storage IO and providers are fakes."""
    _, _, context, authority, _, _ = setup(db, settings)
    settings.conversation_answer_max_input_tokens = 100000
    settings.conversation_answer_graph_tokens = 20000
    bindings = authority.resolve(context)
    decoder, _, _ = repository()
    from app.extraction.kg_extract import template
    def fetch(request):
        result = []
        for anchor in request.anchors:
            item = decoder._decode(anchor, row(anchor), template())
            result.append(replace(item,
                entities=tuple(replace(e, properties=nested_properties()) for e in item.entities),
                relationships=tuple(replace(r, properties=nested_properties()) for r in item.relationships)))
        return tuple(result)
    graph_repo = Mock()
    graph_repo.fetch_anchor_context.side_effect = fetch
    graph_service = GraphRetrievalService(settings, repository=graph_repo, authority=authority)
    nodes = object.__new__(ConversationRagNodes)
    nodes.settings = settings
    nodes.provider = FakeLLMProvider("Synthetic answer [1].")
    nodes.evidence_service = ChatEvidenceService(None, settings, graph_retrieval=graph_service)
    # Keep real build/budget logic, and capture the initial prompt before persistence.
    build = nodes.evidence_service.build
    first_requests = []
    def capture_build(*args, **kwargs):
        plan = build(*args, **kwargs)
        assert len(plan.graph.evidence) == len(bindings) > 0
        first_requests.append(checked_request("q", "q", plan.history, plan.context, plan.graph, settings))
        return plan
    nodes.evidence_service.build = capture_build
    identity = SimpleNamespace(thread_id=uuid4(), turn_id=uuid4(), attempt_no=1)
    turn = SimpleNamespace(question="q")
    rewrite = SimpleNamespace(id=uuid4())
    rewritten = SimpleNamespace(result=SimpleNamespace(standalone_query="q", decision="standalone", history_scope="none"),
        pending_clarification_turn_id=None)
    saved = {"retrieval": (SimpleNamespace(id=uuid4()), SimpleNamespace(rerank_applied=True))}
    snapshots = {}
    repo = SimpleNamespace(db=db, get_snapshot_by_key=lambda _s, _t, _a, key: snapshots[key])
    @contextmanager
    def read(*args):
        yield repo, identity, turn
    nodes._read = read
    nodes._rewrite = lambda *args: (rewrite, rewritten)
    nodes._existing = lambda _r, _i, kind, _p: saved.get(kind)
    # The already retrieved chunks are synthetic; no Hybrid/embedding/reranker call.
    nodes._retrieval = lambda *args: HybridSearchResult("q", len(context.chunks), len(context.chunks), context.chunks)
    def save(_identity, kind, parent, details, values=()):
        ident = uuid4()
        saved[kind] = (SimpleNamespace(id=ident), details.model_copy(deep=True))
        for value in values:
            payload = reorder_objects(value.payload)
            if value.kind == "graph":
                assert payload == value.payload
                original = value.payload["evidence"]["entities"][0]["properties"]
                assert list(payload["evidence"]["entities"][0]["properties"]) != list(original)
            snapshots[value.key] = SimpleNamespace(kind=value.kind, status="available", payload=payload, sources=value.sources)
        return ident
    nodes._save = save
    monkeypatch.setattr("app.rag.conversation_nodes.rehydrate_history", lambda *a: HistoryContext())
    state = {"retrieval_artifact_id": str(saved["retrieval"][0].id)}
    state.update(nodes.build_evidence(state, {}))
    assert saved["evidence"][1].evidence_generation == 1
    assert not nodes.provider.calls
    properties = snapshots["graph_000"].payload["evidence"]["entities"][0]["properties"]
    if corruption == "value":
        properties["provenance"]["unit"] = "Pa"
    elif corruption == "array_order":
        properties["items"].reverse()
    elif corruption == "fingerprint":
        saved["evidence"][1].prompt_fingerprint = "f" * 64
    if corruption:
        with pytest.raises(ConversationError) as error:
            nodes.generate_answer(state, {})
        assert error.value.code == "QA_PROMPT_CHANGED"
        assert not nodes.provider.calls
    else:
        result = nodes.generate_answer(state, {})
        assert result["stage"] == "generated" and len(nodes.provider.calls) == 1
        assert nodes.provider.calls[0].messages == first_requests[0].messages
        assert prompt_fingerprint(nodes.provider.calls[0].messages) == saved["evidence"][1].prompt_fingerprint
