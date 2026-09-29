from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.exc import DBAPIError

from app.db.langgraph import CheckpointPool, CheckpointUnavailable, UPSTREAM_SCHEMA_SHA256
from app.models.qa_message import QAMessage
from app.models.qa_turn_artifact import QATurnArtifact
from app.rag.conversation_graph import ConversationGraph
from app.rag.conversation_state import REWRITE_ARTIFACT_KEY
from app.rag.history_budget import select_history
from app.rag.query_rewrite import QueryRewriter, clarification_text
from app.rag.query_rewrite_prompt import current_rewrite_policy
from app.services.chat_evidence import ChatEvidenceService
from phase13_m2_support import empty_retrieval
from app.schemas.query_rewrite import RewriteArtifactDetails
from app.services.conversation_repository import ConversationError, ConversationRepository
from phase13_m2_support import (
    M2_HEAD, FakeProvider, call, database_engine, factory, invoke, publish,
    riser_response, runtime, session, settings_for, turn,
)
from phase13_support import BASELINE, HEAD, migration


pytestmark = pytest.mark.phase13_integration


@pytest.fixture(autouse=True)
def empty_search(monkeypatch):
    monkeypatch.setattr(ChatEvidenceService, "retrieve", lambda self, *args: empty_retrieval(*args))


@pytest.fixture(scope="module")
def m2_engine(phase13_root_engine):
    engine = database_engine(phase13_root_engine)
    yield engine
    engine.dispose()


@pytest.fixture
def app_graph(m2_engine):
    graph, pool, provider = runtime(m2_engine, FakeProvider(riser_response))
    yield graph, pool, provider
    pool.close()


def rewrite(engine, row):
    artifact = call(engine, lambda repo: repo.get_artifact_by_key(
        row.session_id, row.id, attempt_no=row.attempt_no, key=REWRITE_ARTIFACT_KEY))
    return artifact, RewriteArtifactDetails.model_validate_json(json.dumps(artifact.details)).result


def completed(engine, sid, question="冒口有什么作用？", answer="冒口用于补缩"):
    row = turn(engine, sid, question)
    publish(engine, row, answer)
    return row


def test_compile_write_read_isolation_and_new_connections(m2_engine, app_graph):
    graph, pool, provider = app_graph
    a, b = session(m2_engine), session(m2_engine)
    completed(m2_engine, a)
    row_a, row_b = turn(m2_engine, a, "那它的尺寸呢？"), turn(m2_engine, b, "那它的尺寸呢？")
    state_a, state_b = invoke(graph, row_a), invoke(graph, row_b)
    assert state_a["terminal_status"] == "result_staged"
    assert state_b["outcome"] == "clarification"
    assert state_a["rewrite_artifact_id"] != state_b["rewrite_artifact_id"]
    assert rewrite(m2_engine, row_a)[1].standalone_query == "冒口的尺寸呢？"
    assert rewrite(m2_engine, row_b)[1].standalone_query is None
    assert len(provider.calls) == 2
    pool.close()
    new_graph, new_pool, _ = runtime(m2_engine)
    try:
        assert new_graph.get_state(a).values == state_a
        assert new_graph.get_state(b).values == state_b
    finally:
        new_pool.close()


@pytest.mark.parametrize("question", ["冒口尺寸如何确定？", "铝合金热裂产生的原因是什么？"])
def test_model_standalone_and_topic_switch(m2_engine, app_graph, question):
    graph, _, provider = app_graph
    sid = session(m2_engine)
    completed(m2_engine, sid, "冒口温度800℃如何确定？")
    row = turn(m2_engine, sid, question)
    state = invoke(graph, row)
    assert len(state["history_message_ids"]) == 2 and len(provider.calls) == 1
    assert rewrite(m2_engine, row)[1].standalone_query == question


def test_clarification_survives_graph_and_pool_restart(m2_engine, app_graph):
    graph, pool, provider = app_graph
    sid = session(m2_engine)
    completed(m2_engine, sid, "冒口和冷铁有什么区别？")
    ambiguous = turn(m2_engine, sid, "它的尺寸怎么确定？")
    assert invoke(graph, ambiguous)["outcome"] == "clarification"
    result = rewrite(m2_engine, ambiguous)[1]
    assert result.clarification_options == ["冒口", "冷铁"] and len(provider.calls) == 1
    # M2 stages only; simulate M4 publication through the accepted M1 API.
    assert call(m2_engine, lambda r: r.get_answer(sid, ambiguous.id)) is None
    publish(m2_engine, ambiguous, clarification_text(result), "clarification")
    pool.close()
    graph2, pool2, provider2 = runtime(m2_engine)
    try:
        reply = turn(m2_engine, sid, "冒口")
        assert invoke(graph2, reply)["terminal_status"] == "result_staged"
        assert rewrite(m2_engine, reply)[1].standalone_query == "冒口的尺寸怎么确定？"
        assert len(provider2.calls) == 1
    finally:
        pool2.close()


def test_artifact_commit_before_checkpoint_replays_without_second_llm(m2_engine, app_graph):
    graph, pool, provider = app_graph
    sid = session(m2_engine)
    completed(m2_engine, sid)
    row = turn(m2_engine, sid, "它尺寸如何确定？")
    def crash():
        raise RuntimeError("synthetic crash after artifact commit")
    graph.nodes.after_artifact_commit = crash
    with pytest.raises(RuntimeError, match="synthetic crash"):
        invoke(graph, row)
    artifact = rewrite(m2_engine, row)[0]
    pool.close()
    restarted, pool2, provider2 = runtime(m2_engine)
    try:
        result = restarted.resume(sid, row.id, row.request_id, 1)
        assert result["rewrite_artifact_id"] == str(artifact.id)
        assert result["terminal_status"] == "result_staged"
        assert len(provider.calls) == 1 and not provider2.calls
        assert invoke(restarted, row)["rewrite_artifact_id"] == str(artifact.id)
        assert not provider2.calls
    finally:
        pool2.close()


def test_old_attempt_late_return_cannot_overwrite_artifact_or_checkpoint(m2_engine):
    sid = session(m2_engine)
    completed(m2_engine, sid)
    row = turn(m2_engine, sid, "它尺寸如何确定？")
    entered, release = Event(), Event()
    def delayed(request):
        entered.set()
        assert release.wait(timeout=20)
        return riser_response(request)
    old, old_pool, _ = runtime(m2_engine, FakeProvider(delayed))
    newer, new_pool, _ = runtime(m2_engine, FakeProvider(riser_response))
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(invoke, old, row)
            assert entered.wait(timeout=10)
            call(m2_engine, lambda r: r.transition_turn(sid, row.id, expected_status="running", new_status="needs_recovery", expected_attempt=1, error_code="SYNTHETIC_RETRY"))
            fresh = call(m2_engine, lambda r: r.transition_turn(sid, row.id, expected_status="needs_recovery", new_status="running", expected_attempt=1, rewrite_policy=current_rewrite_policy()))
            assert fresh.attempt_no == 2
            new_state = invoke(newer, fresh)
            checkpoint_id = newer.get_state(sid).config["configurable"]["checkpoint_id"]
            release.set()
            with pytest.raises(ConversationError):
                future.result(timeout=10)
        assert newer.get_state(sid).values == new_state
        assert newer.get_state(sid).config["configurable"]["checkpoint_id"] == checkpoint_id
        assert call(m2_engine, lambda r: r.get_artifact_by_key(sid, row.id, attempt_no=1, key=REWRITE_ARTIFACT_KEY)) is None
    finally:
        release.set()
        old_pool.close()
        new_pool.close()


def test_different_threads_execute_model_calls_concurrently(m2_engine):
    barrier = Barrier(2)
    def response(request):
        barrier.wait(timeout=10)
        return riser_response(request)
    graph, pool, _ = runtime(m2_engine, FakeProvider(response))
    try:
        rows = []
        for _ in range(2):
            sid = session(m2_engine)
            completed(m2_engine, sid)
            rows.append(turn(m2_engine, sid, "它尺寸如何确定？"))
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda row: invoke(graph, row), rows))
        assert {s["thread_id"] for s in results} == {str(r.session_id) for r in rows}
        assert all(s["terminal_status"] == "result_staged" for s in results)
    finally:
        pool.close()


def test_history_excludes_unpublished_and_rejects_foreign_ids(m2_engine, app_graph):
    graph, _, _ = app_graph
    sid, foreign = session(m2_engine), session(m2_engine)
    for _ in range(2):
        completed(m2_engine, sid)
    failed = turn(m2_engine, sid, "FAILED_SECRET")
    call(m2_engine, lambda r: r.transition_turn(sid, failed.id, expected_status="running", new_status="failed", expected_attempt=1, error_code="SYNTHETIC_FAILURE"))
    foreign_turn = completed(m2_engine, foreign, "FOREIGN_SECRET?")
    row = turn(m2_engine, sid, "它尺寸如何确定？")
    context = call(m2_engine, lambda r: select_history(r, row, settings_for(m2_engine)))
    assert len(context.messages) == 4
    assert not any("SECRET" in m.content for m in context.messages)
    foreign_id = call(m2_engine, lambda r: r.get_user_message(foreign, foreign_turn.id)).id
    _, initial = graph._initial(sid, row.id, row.request_id, 1)
    initial["history_message_ids"] = [str(foreign_id)]
    with pytest.raises(ConversationError, match="History"):
        graph.nodes.understand_question(initial, {"configurable": {"thread_id": str(sid)}})
    with pytest.raises(ConversationError):
        graph.nodes.load_context(initial, {"configurable": {"thread_id": str(foreign)}})


@pytest.mark.parametrize("status", ["running", "finalizing", "failed", "needs_recovery"])
def test_all_unpublished_states_are_excluded_even_with_draft(m2_engine, status):
    from app.schemas.conversation_persistence import SnapshotInput
    from app.services.conversation_repository import fingerprint
    sid = session(m2_engine)
    completed(m2_engine, sid)
    unpublished = turn(m2_engine, sid, "UNPUBLISHED_USER")
    artifact = call(m2_engine, lambda r: r.save_artifact(sid, unpublished.id, expected_attempt=1,
        key="draft", kind="generation", input_fingerprint=fingerprint({})))
    call(m2_engine, lambda r: r.save_snapshots(sid, unpublished.id, artifact.id, expected_attempt=1,
        snapshots=(SnapshotInput("draft", "answer_draft", {"text": "UNPUBLISHED_DRAFT", "outcome": "no_context"}),)))
    if status != "running":
        call(m2_engine, lambda r: r.transition_turn(sid, unpublished.id, expected_status="running",
            new_status=status, expected_attempt=1, error_code="SYNTHETIC" if status in {"failed", "needs_recovery"} else None))
    # A future boundary is used only to test the selector, not to create a second
    # unresolved business turn (which M1 correctly forbids).
    from types import SimpleNamespace
    context = call(m2_engine, lambda r: select_history(r, SimpleNamespace(session_id=sid, turn_no=100), settings_for(m2_engine)))
    assert len(context.messages) == 2
    assert all("UNPUBLISHED" not in m.content for m in context.messages)


def test_oversized_latest_turn_does_not_revive_an_old_antecedent(m2_engine):
    sid = session(m2_engine)
    completed(m2_engine, sid, "冒口有什么作用？")
    completed(m2_engine, sid, "冷铁有什么作用？", "冷铁说明" * 2000)
    graph, pool, provider = runtime(m2_engine)
    try:
        row = turn(m2_engine, sid, "它尺寸如何确定？")
        assert invoke(graph, row)["error_code"] == "QA_CONTEXT_BUDGET_EXCEEDED"
        assert call(m2_engine, lambda repo: repo.get_artifact_by_key(sid, row.id, attempt_no=1, key=REWRITE_ARTIFACT_KEY)) is None
        assert not provider.calls
    finally:
        pool.close()


def test_real_fastapi_lifespan_reopens_saved_thread(m2_engine, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.db import session as session_module
    from app.llm import provider as provider_module
    monkeypatch.setattr(session_module, "SessionLocal", factory(m2_engine))
    providers = []
    def build(_):
        provider = FakeProvider(riser_response)
        providers.append(provider)
        return provider
    monkeypatch.setattr(provider_module, "build_llm_provider", build)
    monkeypatch.setattr(PostgresSaver, "setup", lambda *a, **kw: pytest.fail("Runtime DDL"))
    settings = settings_for(m2_engine, conversation_enabled=True)
    sid = session(m2_engine)
    row = turn(m2_engine, sid, "冒口尺寸如何确定？")
    app = create_app(settings=settings)
    with TestClient(app):
        runtime1 = app.state.conversation_graph
        state = invoke(runtime1, row)
    assert providers[0].closed and runtime1.checkpoints.pool is None
    with TestClient(create_app(settings=settings)) as client:
        assert client.app.state.conversation_graph.get_state(sid).values == state
    assert providers[1].closed


def test_checkpoint_remains_bounded_after_long_history(m2_engine, app_graph):
    graph, pool, _ = app_graph
    sid = session(m2_engine)
    sizes = []
    for i in range(30):
        row = turn(m2_engine, sid, "冒口有什么作用？" if i == 0 else "它尺寸如何确定？")
        # Explicit user topic every round prevents repetitive test rewrite drift.
        if i:
            provider = graph.nodes.rewriter.provider
            def result(request):
                data = riser_response(request)
                oldest = next(json.loads(m.content[0].text) for m in request.messages[1:-1]
                              if m.role == "assistant")
                data["referenced_message_ids"] = [oldest["message_id"]]
                data["resolved_references"][0]["source_message_ids"] = [oldest["message_id"]]
                return data
            provider.response = result
        state = invoke(graph, row)
        assert len(state["history_message_ids"]) <= 12
        sizes.append(len(json.dumps(state).encode()))
        publish(m2_engine, row, "冒口 SYNTHETIC_EVIDENCE_SENTINEL_" + str(i))
    assert max(sizes) < 2048 and max(sizes[10:]) - min(sizes[10:]) < 30
    assert len(call(m2_engine, lambda r: r.list_messages(sid, limit=100))) == 60
    with pool.pool.connection() as conn:
        blobs = conn.execute("SELECT blob FROM checkpoint_blobs WHERE thread_id=%s UNION ALL SELECT blob FROM checkpoint_writes WHERE thread_id=%s", (str(sid), str(sid))).fetchall()
        assert blobs and all(b"SYNTHETIC_EVIDENCE_SENTINEL" not in (r["blob"] or b"") for r in blobs)
        tuples = list(pool.saver(sid, factory(m2_engine)).list({"configurable": {"thread_id": str(sid)}}))
        assert len(tuples) >= 30
        assert all(len(json.dumps(t.checkpoint["channel_values"]).encode()) < 4096 for t in tuples)


def test_upstream_schema_matches_frozen_migration(m2_engine):
    assert sha256("\n".join(BasePostgresSaver.MIGRATIONS).encode()).hexdigest() == UPSTREAM_SCHEMA_SHA256
    inspector = inspect(m2_engine)
    assert set(inspector.get_table_names(schema="langgraph_checkpoints")) == {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
    columns = {c["name"]: c for c in inspector.get_columns("checkpoint_blobs", schema="langgraph_checkpoints")}
    assert columns["blob"]["nullable"]
    assert "task_path" in {c["name"] for c in inspector.get_columns("checkpoint_writes", schema="langgraph_checkpoints")}


def test_migration_0008_0009_0010_preserves_nonempty_history(phase13_root_engine):
    engine = database_engine(phase13_root_engine, revision=BASELINE)
    sid, mid = uuid4(), uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO qa_sessions(id,title) VALUES (:id,'legacy')"), {"id": sid})
            conn.execute(text("INSERT INTO qa_messages(id,session_id,role,content) VALUES (:id,:sid,'user','OLD_MESSAGE')"), {"id": mid, "sid": sid})
            migration(conn, HEAD)
            migration(conn, M2_HEAD)
        with engine.connect() as conn:
            row = conn.execute(text("SELECT content,sequence_no,turn_id FROM qa_messages WHERE id=:id"), {"id": mid}).one()
            assert tuple(row) == ("OLD_MESSAGE", 1, None)
        graph, pool, _ = runtime(engine)
        try:
            assert invoke(graph, turn(engine, sid, "冒口尺寸如何确定？"))["terminal_status"] == "result_staged"
        finally:
            pool.close()
    finally:
        engine.dispose()


def test_no_0010_fails_without_ddl_and_empty_downgrade_is_safe(phase13_root_engine, monkeypatch):
    engine = database_engine(phase13_root_engine, revision=HEAD)
    def forbidden(*args, **kwargs):
        raise AssertionError("runtime must not setup")
    monkeypatch.setattr(PostgresSaver, "setup", forbidden)
    checkpoints = CheckpointPool(settings_for(engine))
    try:
        with pytest.raises(CheckpointUnavailable, match="NOT_READY"):
            checkpoints.open()
        assert checkpoints.pool is None
        assert "langgraph_checkpoints" not in inspect(engine).get_schema_names()
        sid = session(engine)
        with engine.begin() as conn:
            migration(conn, M2_HEAD)
        checkpoints.open()
        checkpoints.close()
        with engine.begin() as conn:
            migration(conn, HEAD, downgrade=True)
        assert call(engine, lambda r: r.get_session(sid)).id == sid
        assert "langgraph_checkpoints" not in inspect(engine).get_schema_names()
        with engine.begin() as conn:
            migration(conn, M2_HEAD)
        checkpoints.open()
    finally:
        checkpoints.close()
        engine.dispose()


def test_populated_checkpoint_downgrade_refuses_and_rolls_back(m2_engine, app_graph):
    graph, _, _ = app_graph
    sid = session(m2_engine)
    state = invoke(graph, turn(m2_engine, sid, "冒口尺寸如何确定？"))
    with pytest.raises(DBAPIError, match="downgrade refused"):
        with m2_engine.begin() as conn:
            migration(conn, HEAD, downgrade=True)
    assert graph.get_state(sid).values == state
    with m2_engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == M2_HEAD
