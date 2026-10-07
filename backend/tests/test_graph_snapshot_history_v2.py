"""History and retry policy: preserve bodies and never decode unsupported graph snapshots."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.services.conversation_history import published_answer
from app.services.conversation_repository import ConversationError
from app.services import conversation_recovery as recovery
from app.schemas.conversation_rag import EvidenceDetails, GenerationDetails, ResultDetails, read_graph_payload
from app.schemas.conversation_persistence import EvidenceSourceRef


def test_old_snapshot_history_keeps_answer_and_marks_unsupported_without_reparse():
    sid,tid,rid,gid,eid,snapshot,draft,did,cid=[uuid4() for _ in range(9)]
    turn=SimpleNamespace(session_id=sid,id=tid,status="completed",outcome="answer",question="q",attempt_no=1)
    details=EvidenceDetails(evidence_generation=1,citation_keys=[],graph_keys=["g"],history_message_ids=[],
        graph_enabled=True,graph_triggered=True,graph_truncated=False,input_estimated_tokens=1,prompt_fingerprint="a"*64)
    artifacts={
        rid:SimpleNamespace(id=rid,kind="result",schema_version=1,attempt_no=1,details=ResultDetails(
            outcome="answer",generation_artifact_id=gid,evidence_artifact_id=eid).model_dump(mode="json")),
        gid:SimpleNamespace(id=gid,kind="generation",schema_version=1,attempt_no=1,details=GenerationDetails(
            outcome="answer",snapshot_keys=["answer"]).model_dump(mode="json")),
        eid:SimpleNamespace(id=eid,kind="evidence",schema_version=1,attempt_no=1,details=details.model_dump(mode="json"))}
    payload={"evidence":{"document":{"doc_id":"legacy"},"table":{"table_ref":"legacy"},"unparseable":"retained"}}
    original=deepcopy(payload)
    view=SimpleNamespace(id=snapshot,kind="graph",status="available",sources=(EvidenceSourceRef(did,cid),),payload=payload)
    repo=SimpleNamespace(get_answer=lambda *_:SimpleNamespace(content="历史回答原文",answer_snapshot_id=draft),
        get_artifact_by_key=lambda *a,**k:artifacts[rid], get_artifact=lambda _s,_t,i:artifacts[i],
        get_snapshot_by_key=lambda _s,_t,a,_k:SimpleNamespace(id=draft) if a==gid else view)
    result=published_answer(repo,turn)
    assert result.answer=="历史回答原文" and not result.graph.evidence
    assert result.sources[0].status=="unsupported_version" and payload==original


@pytest.mark.parametrize("version",[None,1,3,"2"])
def test_only_explicit_v2_snapshot_is_executable(version):
    payload={"schema_version":version,"evidence":{}}
    with pytest.raises(ConversationError) as e: read_graph_payload(payload)
    assert e.value.code=="QA_GRAPH_EVIDENCE_VERSION_UNSUPPORTED"


def test_unsupported_version_releases_turn_and_cannot_retry_same_request():
    turn=SimpleNamespace(status="running",session_id=uuid4(),id=uuid4(),attempt_no=1,turn_no=1,
                         error_code="QA_GRAPH_EVIDENCE_VERSION_UNSUPPORTED")
    calls=[]
    repo=SimpleNamespace(transition_turn=lambda *a,**k:calls.append(k),
        has_later_turn=lambda *a:False,require_rewrite_policy=lambda *a:None)
    recovery.record_failure(repo,turn,turn.error_code)
    assert calls[0]["new_status"]=="failed"
    turn.status="failed"
    assert not recovery.can_retry(repo,turn)
    with pytest.raises(ConversationError) as e: recovery.prepare_retry(repo,turn,SimpleNamespace(values={}))
    assert e.value.code==turn.error_code

def test_hidden_graph_preserves_independent_citation_number(monkeypatch):
    from dataclasses import replace
    from app.rag.context_builder import build_rag_context
    from app.rag.graph_context_builder import build_graph_context
    from app.graph.models import GraphRetrievalResult
    from app.schemas.conversation_rag import CitationPayload, GraphPayload
    from app.rag.conversation_nodes import graph_refs, source_refs
    from graph_v2_support import binding, item, settings
    from test_graph_context import evidence
    from test_rag_graph_fusion import result as search_result
    sid,tid,rid,gid,eid,draft=[uuid4() for _ in range(6)]
    turn=SimpleNamespace(session_id=sid,id=tid,status="completed",outcome="answer",question="q",attempt_no=1)
    context=build_rag_context("q",search_result([item(),item(2)]),settings())
    c1,c2=context.chunks
    a=binding(graph="G1",end=len(c1.content))
    a=replace(a,provenance=(replace(a.provenance[0],chunk_set_id=c1.chunk_set_id),))
    graph=build_graph_context(GraphRetrievalResult((evidence(a),)))
    details=EvidenceDetails(evidence_generation=1,citation_keys=["c1","c2"],graph_keys=["g"],history_message_ids=[],
        graph_enabled=True,graph_triggered=True,graph_truncated=False,graph_schema_version=2,
        graph_diagnostics=graph.diagnostics,input_estimated_tokens=1,prompt_fingerprint="a"*64)
    artifacts={
        rid:SimpleNamespace(id=rid,kind="result",schema_version=1,attempt_no=1,details=ResultDetails(
            outcome="answer",generation_artifact_id=gid,evidence_artifact_id=eid).model_dump(mode="json")),
        gid:SimpleNamespace(id=gid,kind="generation",schema_version=1,attempt_no=1,details=GenerationDetails(
            outcome="answer",snapshot_keys=["answer"]).model_dump(mode="json")),
        eid:SimpleNamespace(id=eid,kind="evidence",schema_version=1,attempt_no=1,details=details.model_dump(mode="json"))}
    views={
        "c1":SimpleNamespace(id=uuid4(),kind="citation",status="source_deleted",sources=source_refs(c1),payload=None),
        "c2":SimpleNamespace(id=uuid4(),kind="citation",status="available",sources=source_refs(c2),
            payload=CitationPayload(chunk=c2).model_dump(mode="json")),
        # Independently hidden citations must suppress even an otherwise available graph snapshot.
        "g":SimpleNamespace(id=uuid4(),kind="graph",status="available",sources=graph_refs(graph.evidence[0]),
            payload=GraphPayload(evidence=graph.evidence[0]).model_dump(mode="json"))}
    repo=SimpleNamespace(get_answer=lambda *_:SimpleNamespace(content="历史回答原文",answer_snapshot_id=draft),
        get_artifact_by_key=lambda *a,**k:artifacts[rid],get_artifact=lambda _s,_t,i:artifacts[i],
        get_snapshot_by_key=lambda _s,_t,a,k:SimpleNamespace(id=draft) if a==gid else views[k])
    before=deepcopy(views["g"].payload)
    answer=published_answer(repo,turn)
    assert answer.answer=="历史回答原文" and [c.citation_id for c in answer.citations]==[2]
    assert not answer.graph.evidence and not answer.graph.diagnostics[0].facts_used
    assert answer.graph.diagnostics[0].use_status=="source_invalid" and views["g"].payload==before
