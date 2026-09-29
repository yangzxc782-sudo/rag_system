"""LLM v2 policy, failures and published legacy history on isolated PostgreSQL."""
import json
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import func, select

from app.core.errors import BusinessError
from app.models.qa_message import QAMessage
from app.models.qa_turn import QATurn
from app.models.qa_turn_artifact import QATurnArtifact
from app.rag.conversation_state import StateContract, StateContractV2
from app.rag.query_rewrite_prompt import current_rewrite_policy
from app.schemas.conversations import TurnCreateRequest
from app.schemas.query_rewrite import REWRITE_ARTIFACT_KEY, REWRITE_POLICY_KEY
from app.services.conversation_repository import ConversationError, ConversationRepository, fingerprint
from app.services.conversations import Conversations
from phase13_m2_support import call, database_engine, factory, publish, session
from phase13_m3_support import install_search, runtime, source

pytestmark = pytest.mark.phase13_integration


@pytest.fixture(scope="module")
def policy_engine(phase13_root_engine):
    engine = database_engine(phase13_root_engine)
    yield engine
    engine.dispose()


@pytest.fixture
def policy_chat(policy_engine, monkeypatch):
    search = install_search(monkeypatch, [source(policy_engine)])
    graph, pool, provider = runtime(policy_engine)
    service = Conversations(graph)
    yield service, graph, pool, provider, search
    service.close()
    pool.close()


def legacy_artifact(engine, row, *, clarify=False):
    """Historical data fixture, with no old QueryRewriter/Graph execution."""
    details = {"artifact_type": "query_rewrite",
        "result": {"decision": "clarify" if clarify else "standalone",
            "standalone_query": None if clarify else row.question,
            "clarification_reason": "missing_antecedent" if clarify else None,
            "clarification_options": ["WCB", "QT500-7"] if clarify else [],
            "history_scope": "none", "referenced_message_ids": [], "resolved_references": []},
        "history_message_ids": [], "pending_clarification_turn_id": None,
        "budget_basis": "estimated_utf8_bytes_v1", "input_bytes": 0, "input_estimated_tokens": 0,
        "validation_codes": ["fast_path"], "metrics": {}}
    with factory(engine)() as db, db.begin():
        saved = QATurnArtifact(id=uuid4(), session_id=row.session_id, turn_id=row.id, attempt_no=1,
            artifact_key="query_rewrite:v1", kind="rewrite", schema_version=1,
            input_fingerprint=fingerprint({"legacy": str(row.id)}),
            content_fingerprint=fingerprint(details), details=details)
        db.add(saved)
        return saved.id, details


def legacy_checkpoint(engine, pool, row, version, artifact_id=None, terminal=False):
    user = call(engine, lambda r: r.get_user_message(row.session_id, row.id))
    values = dict(thread_id=str(row.session_id), turn_id=str(row.id), request_id=str(row.request_id),
        current_message_id=str(user.id), attempt_no=1, input_fingerprint=row.request_fingerprint,
        rewrite_artifact_id=str(artifact_id) if artifact_id else None)
    if version == 1:
        values = StateContract(**values).model_dump()
    else:
        values = StateContractV2(**values, evidence_generation=1).model_dump()
        if terminal:
            values.update(stage="result_staged", terminal_status="result_staged", outcome="no_context",
                          result_artifact_id=str(uuid4()))
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = values
    checkpoint["channel_versions"] = {key: "1" for key in values}
    # Seed a persisted legacy record directly; never compile an old graph.
    PostgresSaver(pool.pool).put({"configurable": {"thread_id": str(row.session_id), "checkpoint_ns": ""}},
        checkpoint, {"source": "input", "step": 0, "parents": {}}, checkpoint["channel_versions"])
    return values


@pytest.mark.parametrize("footprint", ["none", "rewrite", "checkpoint_v1", "checkpoint_v2", "terminal_checkpoint"])
@pytest.mark.parametrize("status", ["running", "finalizing", "needs_recovery", "failed"])
def test_every_incomplete_legacy_execution_is_nonretryable(policy_engine, policy_chat, footprint, status):
    service, graph, pool, provider, search = policy_chat
    sid = session(policy_engine)
    request = TurnCreateRequest(request_id=uuid4(), question="旧问题如何处理？")
    row = call(policy_engine, lambda r: r.start_turn(sid, request.request_id, request.question))
    ident = None
    if footprint != "none":
        ident, old_details = legacy_artifact(policy_engine, row)
    if footprint.startswith("checkpoint") or footprint == "terminal_checkpoint":
        legacy_checkpoint(policy_engine, pool, row, 1 if footprint == "checkpoint_v1" else 2,
                          ident, footprint == "terminal_checkpoint")
        assert graph.get_state(sid).values["turn_id"] == str(row.id)
    if status != "running":
        call(policy_engine, lambda r: r.transition_turn(sid, row.id, expected_status="running", new_status=status,
            expected_attempt=1, error_code="QA_EXECUTION_INTERRUPTED" if status in {"failed", "needs_recovery"} else None))
    if status == "running":
        for action in (graph.invoke, graph.resume, graph.get_result):
            with pytest.raises(ConversationError) as caught:
                action(sid, row.id, row.request_id, 1)
            assert caught.value.code == "QA_REWRITE_STRATEGY_UNSUPPORTED"
    observed = service.request_status(sid, request.request_id)
    assert observed.status == "failed" and not observed.can_retry
    assert observed.error_code == "QA_REWRITE_STRATEGY_UNSUPPORTED"
    with pytest.raises(ConversationError) as caught:
        service.submit(sid, request)
    assert caught.value.code == "QA_REWRITE_STRATEGY_UNSUPPORTED"
    assert call(policy_engine, lambda r: r.get_turn(sid, row.id)).attempt_no == 1
    assert call(policy_engine, lambda r: r.get_answer(sid, row.id)) is None
    for key in (REWRITE_POLICY_KEY, REWRITE_ARTIFACT_KEY):
        assert call(policy_engine, lambda r: r.get_artifact_by_key(sid, row.id, attempt_no=1, key=key)) is None
    if ident:
        assert call(policy_engine, lambda r: r.get_artifact(sid, row.id, ident)).details == old_details
    assert not provider.calls and not search.calls


def test_acceptance_policy_marker_and_user_message_rollback_together(policy_engine, policy_chat, monkeypatch):
    service, _, _, provider, search = policy_chat
    sid = session(policy_engine)
    def fail(*args):
        raise RuntimeError("synthetic policy commit failure")
    monkeypatch.setattr(ConversationRepository, "save_rewrite_policy", fail)
    with pytest.raises(RuntimeError):
        service.submit(sid, TurnCreateRequest(request_id=uuid4(), question="问题？"))
    with factory(policy_engine)() as db:
        assert db.scalar(select(func.count()).select_from(QATurn).where(QATurn.session_id == sid)) == 0
        assert db.scalar(select(func.count()).select_from(QAMessage).where(QAMessage.session_id == sid)) == 0
    assert not provider.calls and not search.calls


@pytest.mark.parametrize("failure,code", [
    ("invalid json", "QA_REWRITE_OUTPUT_INVALID"),
    ({"decision": "standalone", "standalone_query": 42}, "QA_REWRITE_OUTPUT_INVALID"),
    (TimeoutError("sensitive body"), "LLM_TIMEOUT"),
    (BusinessError("LLM_UNAVAILABLE", "safe", status_code=503), "LLM_UNAVAILABLE"),
])
def test_model_failure_is_failed_and_explicit_retry_uses_new_attempt(policy_engine, policy_chat, failure, code):
    service, graph, _, provider, search = policy_chat
    sid = session(policy_engine)
    request = TurnCreateRequest(request_id=uuid4(), question="完整首轮问题？")
    original = provider.rewrite
    def respond(_):
        if isinstance(failure, Exception):
            raise failure
        return failure
    provider.rewrite = respond
    with pytest.raises(ConversationError) as caught:
        service.submit(sid, request)
    assert caught.value.code == code
    status = service.request_status(sid, request.request_id)
    assert status.status == "failed" and status.can_retry and status.result is None and status.outcome is None
    assert status.assistant_message_id is None and not search.calls
    assert graph.get_state(sid).values["error_code"] == code
    provider.rewrite = original
    assert service.submit(sid, request)[1].status == "completed"
    turn = call(policy_engine, lambda r: r.get_turn(sid, status.turn_id))
    assert turn.attempt_no == 2 and len(provider.rewrite_calls) == 2
    assert call(policy_engine, lambda r: r.get_artifact_by_key(sid, turn.id, attempt_no=1, key=REWRITE_ARTIFACT_KEY)) is None
    for attempt in (1, 2):
        assert call(policy_engine, lambda r: r.get_artifact_by_key(sid, turn.id, attempt_no=attempt, key=REWRITE_POLICY_KEY))
    before = len(provider.calls)
    assert service.submit(sid, request)[1].status == "completed"
    assert len(provider.calls) == before


def test_published_legacy_clarification_remains_readable_and_new_llm_resolves_reply(policy_engine, policy_chat):
    service, _, _, provider, search = policy_chat
    sid = session(policy_engine)
    old = call(policy_engine, lambda r: r.start_turn(sid, uuid4(), "它的成分有哪些？"))
    artifact_id, old_details = legacy_artifact(policy_engine, old, clarify=True)
    publish(policy_engine, old, "请补充材料对象。", "clarification")
    assert service.messages(sid).items[-1].content == "请补充材料对象。"
    def respond(request):
        payload = json.loads(request.messages[-1].content[0].text)
        assert payload["pending_clarification"]["question"] == old.question
        assert payload["question"] == "WCB"
        return {"decision": "rewritten", "standalone_query": "WCB的化学成分包括哪些元素？",
                "history_scope": "clarification", "resolved_references": []}
    provider.rewrite = respond
    reply = TurnCreateRequest(request_id=uuid4(), question="WCB")
    assert service.submit(sid, reply)[1].outcome == "answer"
    assert len(provider.rewrite_calls) == 1 and search.calls[-1]["query"] == "WCB的化学成分包括哪些元素？"
    assert call(policy_engine, lambda r: r.get_artifact(sid, old.id, artifact_id)).details == old_details
    assert service.request_status(sid, old.request_id).status == "completed"


def test_model_new_clarification_does_not_inherit_previous_pending_root(policy_engine, policy_chat):
    service, _, _, provider, _ = policy_chat
    sid = session(policy_engine)
    provider.rewrite = lambda _: {"decision": "clarify", "history_scope": "none", "clarification_reason": "请补充对象。"}
    first = service.submit(sid, TurnCreateRequest(request_id=uuid4(), question="第一件事？"))[1]
    second = service.submit(sid, TurnCreateRequest(request_id=uuid4(), question="现在讨论另一件事？"))[1]
    artifact = call(policy_engine, lambda r: r.get_artifact_by_key(sid, second.turn_id, attempt_no=1, key=REWRITE_ARTIFACT_KEY))
    assert artifact.details["input_pending_clarification_turn_id"] == str(first.turn_id)
    assert "pending_clarification_turn_id" not in artifact.details
    def respond(request):
        payload = json.loads(request.messages[-1].content[0].text)
        assert payload["pending_clarification"]["question"] == "现在讨论另一件事？"
        return {"decision": "rewritten", "standalone_query": "第二件事的完整查询", "history_scope": "clarification"}
    provider.rewrite = respond
    assert service.submit(sid, TurnCreateRequest(request_id=uuid4(), question="补充说明"))[1].outcome == "answer"
