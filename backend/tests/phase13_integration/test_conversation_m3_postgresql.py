"""Real PostgreSQL/Checkpointer contracts; all model/search services are synthetic."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Event
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select

from app.core.errors import BusinessError, LLM_TIMEOUT
from app.rag.query_rewrite_prompt import current_rewrite_policy
from app.models.qa_evidence_snapshot import QAEvidenceSnapshot
from app.models.qa_turn_artifact import QATurnArtifact
from app.models.retrieval_log import RetrievalLog
from app.rag.conversation_nodes import STAGE_KEYS
from app.rag.conversation_prompt import prompt_cost
from app.services.conversation_repository import ConversationError, ConversationRepository
from phase13_m2_support import call, database_engine, factory, invoke, publish, session, turn
from phase13_m3_support import (ANSWER, EVIDENCE, ChatProvider, install_search, publish_staged,
                               redact, result, runtime, source)
from test_rag_reranking import FakeRerankingService


pytestmark = pytest.mark.phase13_integration


@pytest.fixture(scope="module")
def m3_engine(phase13_root_engine):
    engine = database_engine(phase13_root_engine)
    yield engine
    engine.dispose()


@pytest.fixture
def chat(m3_engine, monkeypatch):
    sources = [source(m3_engine, n=i) for i in (1, 2)]
    search = install_search(monkeypatch, sources)
    graph, pool, provider = runtime(m3_engine)
    yield graph, pool, provider, search
    pool.close()


def artifact(engine, row, stage):
    return call(engine, lambda r: r.get_artifact_by_key(row.session_id, row.id,
        attempt_no=row.attempt_no, key=STAGE_KEYS[stage]))


def test_complete_graph_stages_once_without_publishing(m3_engine, chat):
    graph, pool, provider, search = chat
    row = turn(m3_engine, session(m3_engine), "冒口有什么作用？")
    state = invoke(graph, row)
    assert state["terminal_status"] == "result_staged", state
    assert state["state_schema_version"] == 2 and state["outcome"] == "answer"
    staged = result(graph, row)
    assert staged.checkpoint_complete and staged.answer == ANSWER and len(staged.citations) == 2
    assert len(provider.rewrite_calls) == 1 and len(provider.answer_calls) == 1
    assert search.embedding.encode_query_calls == [row.question] and len(search.search_calls) == 2
    assert call(m3_engine, lambda r: r.get_answer(row.session_id, row.id)) is None
    assert call(m3_engine, lambda r: r.get_turn(row.session_id, row.id)).status == "running"
    assert len(call(m3_engine, lambda r: r.list_messages(row.session_id))) == 1
    assert all(artifact(m3_engine, row, stage) is not None for stage in STAGE_KEYS)
    with factory(m3_engine)() as db:
        assert db.scalar(select(func.count()).select_from(RetrievalLog).where(RetrievalLog.turn_id == row.id)) == 1
        assert EVIDENCE not in json.dumps([a.details for a in db.scalars(select(QATurnArtifact).where(QATurnArtifact.turn_id == row.id))])
    assert invoke(graph, row) == state  # Replay complete stages, same immutable IDs.
    assert len(provider.answer_calls) == len(search.calls) == 1
    assert publish_staged(m3_engine, graph, row).content == ANSWER


@pytest.mark.parametrize("followup,expected", [("那它尺寸怎么确定？", "冒口尺寸怎么确定？"),
    ("有哪些限制条件？", "冒口有哪些限制条件？"), ("铝合金热裂产生的原因是什么？", "铝合金热裂产生的原因是什么？")])
def test_followup_query_original_prompt_and_filter_reset(m3_engine, chat, followup, expected):
    graph, _, provider, search = chat
    sid = session(m3_engine)
    first = call(m3_engine, lambda r: r.start_turn(sid, uuid4(), "冒口有什么作用？", limit=1, rewrite_policy=current_rewrite_policy(),
                 document_id=UUID(search.sources[0]["document_id"])))
    assert invoke(graph, first)["outcome"] == "answer"
    publish_staged(m3_engine, graph, first)
    current = call(m3_engine, lambda r: r.start_turn(sid, uuid4(), followup, limit=2, rewrite_policy=current_rewrite_policy()))
    state = invoke(graph, current)
    assert state["terminal_status"] == "result_staged", state
    assert search.calls[-1]["query"] == expected
    assert search.calls[-1]["document_id"] is None and search.calls[-1]["limit"] == 2
    prompt = provider.answer_calls[-1]
    assert followup in prompt.messages[-1].content[0].text and expected in prompt.messages[-1].content[0].text
    if followup.startswith("铝"):
        assert len(state["history_message_ids"]) == 2
        assert len(prompt.messages) == 2
    else:
        assert len(provider.rewrite_calls) == 2 and len(state["history_message_ids"]) == 2
        assert "[1]" not in prompt.messages[2].content[0].text
        assert "历史引用1" in prompt.messages[2].content[0].text
    assert [c.citation_id for c in result(graph, current).citations] == [1, 2]


def test_clarification_no_retrieval_and_restart_answer(m3_engine, chat):
    graph, pool, provider, search = chat
    sid = session(m3_engine)
    prior = turn(m3_engine, sid, "冒口和冷铁有什么区别？")
    publish(m3_engine, prior)
    row = turn(m3_engine, sid, "它的尺寸怎么确定？")
    state = invoke(graph, row)
    assert state["outcome"] == "clarification" and state["terminal_status"] == "result_staged", state
    assert state["pending_clarification_turn_id"] == str(row.id)
    assert "冒口" in result(graph, row).answer and "冷铁" in result(graph, row).answer
    assert not search.calls and len(provider.rewrite_calls) == 1 and not provider.answer_calls
    publish_staged(m3_engine, graph, row)
    pool.close()
    fresh, pool2, provider2 = runtime(m3_engine)
    try:
        reply = turn(m3_engine, sid, "冒口")
        assert invoke(fresh, reply)["outcome"] == "answer"
        assert search.calls[-1]["query"] == "冒口的尺寸怎么确定？"
        assert len(provider2.rewrite_calls) == 1
    finally:
        pool2.close()


def test_thread_isolation_and_reinitialize(m3_engine, chat):
    graph, pool, _, search = chat
    a, b = session(m3_engine), session(m3_engine)
    first = turn(m3_engine, a, "冒口有什么作用？")
    invoke(graph, first)
    publish_staged(m3_engine, graph, first)
    ra, rb = turn(m3_engine, a, "那它的尺寸呢？"), turn(m3_engine, b, "那它的尺寸呢？")
    sa, sb = invoke(graph, ra), invoke(graph, rb)
    assert sa["outcome"] == "answer" and sb["outcome"] == "clarification"
    assert len(search.calls) == 2 and sb["retrieval_artifact_id"] is None
    with pytest.raises(ConversationError):
        graph.get_result(b, ra.id, ra.request_id, 1)
    # Even a valid foreign snapshot / artifact UUID never bypasses the thread scope.
    with pytest.raises(ConversationError):
        call(m3_engine, lambda r: r.get_snapshot(b, rb.id, result(graph, ra).draft_snapshot_id))
    pool.close()
    new, new_pool, _ = runtime(m3_engine)
    try:
        assert new.get_state(a).values == sa and new.get_state(b).values == sb
        assert result(new, ra).checkpoint_complete and result(new, rb).checkpoint_complete
    finally:
        new_pool.close()


def test_no_current_context_does_not_generate(m3_engine, chat):
    graph, _, provider, search = chat
    search.sources.clear()
    row = turn(m3_engine, session(m3_engine), "冒口有什么作用？")
    state = invoke(graph, row)
    assert state["outcome"] == "no_context" and state["terminal_status"] == "result_staged", state
    assert result(graph, row).citations == () and len(provider.rewrite_calls) == 1 and not provider.answer_calls


@pytest.mark.parametrize("stage", ["retrieval", "evidence", "generation", "result"])
def test_commit_then_crash_replays_without_repeating_saved_work(m3_engine, chat, stage):
    graph, pool, provider, search = chat
    row = turn(m3_engine, session(m3_engine), "冒口有什么作用？")
    def crash(current):
        if current == stage:
            raise RuntimeError("synthetic stage crash")
    graph.rag_nodes.after_stage_commit = crash
    with pytest.raises(RuntimeError, match="synthetic stage crash"):
        invoke(graph, row)
    assert artifact(m3_engine, row, stage)
    if stage == "result":
        assert not result(graph, row).checkpoint_complete
    calls = len(provider.answer_calls)
    pool.close()
    new, pool2, provider2 = runtime(m3_engine)
    try:
        state = new.resume(row.session_id, row.id, row.request_id, row.attempt_no)
        assert state["terminal_status"] == "result_staged", state
        assert len(search.calls) == 1
        assert calls + len(provider2.answer_calls) == 1
        assert result(new, row).checkpoint_complete
    finally:
        pool2.close()


@pytest.mark.parametrize("enabled,k,c,failure", [(False, 2, 4, None), (True, 2, 4, None),
    (True, 4, 2, None), (True, 2, 4, "busy"), (True, 2, 4, "timeout"),
    (True, 2, 4, RuntimeError("synthetic reranker failure"))])
def test_real_graph_frozen_once_hybrid_contract(m3_engine, monkeypatch, enabled, k, c, failure):
    from app.services import rag
    sources = [source(m3_engine, n=i) for i in range(4)]
    search = install_search(monkeypatch, sources)
    scorer = FakeRerankingService(failure)
    monkeypatch.setattr(rag.reranking_service, "get_reranking_service", lambda *_: scorer)
    graph, pool, _ = runtime(m3_engine, reranker_enabled=enabled, reranker_candidate_limit=c)
    try:
        row = call(m3_engine, lambda r: r.start_turn(session(m3_engine), uuid4(), "冒口有什么作用？", limit=k, rewrite_policy=current_rewrite_policy()))
        state = invoke(graph, row)
        assert state["outcome"] == "answer", state
        assert len(search.calls) == 1 and search.calls[0]["limit"] == (c if enabled and k <= c else k)
        assert len(scorer.calls) == int(enabled and k <= c)
        detail = artifact(m3_engine, row, "retrieval").details
        assert detail["rerank_applied"] == (enabled and k <= c and failure is None)
        assert len(result(graph, row).citations) == k
        if detail["rerank_applied"]:
            assert result(graph, row).citations[0].chunk_id == sources[-1]["chunk_id"]
        assert all(cite.hybrid_score < 1 for cite in result(graph, row).citations)
    finally:
        pool.close()


# V2 graph budget/replay moved to test_graph_sources_v2.py and the separately
# gated test_graph_v2_postgresql.py. Legacy shared Document/Table fixtures retired.


@pytest.mark.parametrize("failure", ["embedding", "search", "generation"])
def test_required_external_failures_never_become_normal_answers(m3_engine, chat, failure):
    graph, _, provider, search = chat
    if failure == "embedding":
        search.embedding.encode_query = Mock(side_effect=RuntimeError("synthetic embedding error"))
    elif failure == "search":
        search.error = RuntimeError("synthetic search error")
    else:
        provider.answer = BusinessError(LLM_TIMEOUT, "synthetic timeout", status_code=504)
    row = turn(m3_engine, session(m3_engine), "冒口有什么作用？")
    state = invoke(graph, row)
    assert state["terminal_status"] == "needs_recovery" and state["error_code"], state
    assert state["outcome"] is None and artifact(m3_engine, row, "result") is None
    assert call(m3_engine, lambda r: r.get_answer(row.session_id, row.id)) is None


@pytest.mark.parametrize("when", ["retrieval", "retrieval_commit", "evidence", "generation", "result", "during_llm"])
def test_deleted_source_never_reappears_in_replay_or_draft(m3_engine, chat, when):
    graph, _, provider, search = chat
    row = turn(m3_engine, session(m3_engine), "冒口有什么作用？")
    did = search.sources[0]["document_id"]
    if when == "retrieval":
        search.after = lambda: redact(m3_engine, did)
    elif when == "during_llm":
        provider.answer = lambda _: (redact(m3_engine, did), ANSWER)[1]
    else:
        def remove(stage):
            if stage == ("retrieval" if when == "retrieval_commit" else when):
                redact(m3_engine, did)
        graph.rag_nodes.after_stage_commit = remove
    state = invoke(graph, row)
    if when == "result":
        assert state["terminal_status"] == "result_staged"
        with pytest.raises(ConversationError, match="available"):
            result(graph, row)
    else:
        assert state["terminal_status"] == "needs_recovery", state
    assert len(search.calls) == 1
    assert call(m3_engine, lambda r: r.get_answer(row.session_id, row.id)) is None
    graph.rag_nodes.after_stage_commit, search.after = None, None
    # An invalidated saved artifact requires a distinct new attempt, not another Hybrid in this one.
    if artifact(m3_engine, row, "retrieval") is not None:
        assert invoke(graph, row)["terminal_status"] == "needs_recovery"
        assert len(search.calls) == 1
    with factory(m3_engine)() as db:
        drafts = db.scalars(select(QAEvidenceSnapshot).where(QAEvidenceSnapshot.turn_id == row.id,
                              QAEvidenceSnapshot.kind == "answer_draft")).all()
        assert not drafts or all(d.payload is None for d in drafts)


def test_deletion_filter_precedes_bge_and_only_valid_sources_used(m3_engine, monkeypatch):
    from app.services import rag
    rows = [source(m3_engine, n=i) for i in (1, 2)]
    redact(m3_engine, rows[0]["document_id"])
    search = install_search(monkeypatch, rows)
    scorer = FakeRerankingService()
    monkeypatch.setattr(rag.reranking_service, "get_reranking_service", lambda *_: scorer)
    graph, pool, _ = runtime(m3_engine, reranker_enabled=True, reranker_candidate_limit=4)
    try:
        row = call(m3_engine, lambda r: r.start_turn(session(m3_engine), uuid4(), "冒口有什么作用？", limit=2, rewrite_policy=current_rewrite_policy()))
        assert invoke(graph, row)["outcome"] == "answer"
        assert [c.chunk_id for c in scorer.calls[0].candidates] == [rows[1]["chunk_id"]]
        assert [c.document_id for c in result(graph, row).citations] == [rows[1]["document_id"]]
        assert len(search.calls) == 1
    finally:
        pool.close()


def test_old_attempt_late_generation_cannot_overwrite_newer_state(m3_engine, chat):
    graph, _, provider, search = chat
    row = turn(m3_engine, session(m3_engine), "冒口有什么作用？")
    entered, release = Event(), Event()
    def delayed(_):
        entered.set()
        assert release.wait(timeout=20)
        return "OLD_ATTEMPT_ANSWER"
    provider.answer = delayed
    new, pool2, _ = runtime(m3_engine)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            late = executor.submit(invoke, graph, row)
            assert entered.wait(timeout=10)
            call(m3_engine, lambda r: r.transition_turn(row.session_id, row.id, expected_status="running",
                new_status="needs_recovery", expected_attempt=1, error_code="SYNTHETIC_RETRY"))
            fresh = call(m3_engine, lambda r: r.transition_turn(row.session_id, row.id, expected_status="needs_recovery",
                new_status="running", expected_attempt=1, rewrite_policy=current_rewrite_policy()))
            assert fresh.attempt_no == 2
            saved = invoke(new, fresh)
            checkpoint = new.get_state(row.session_id).config["configurable"]["checkpoint_id"]
            release.set()
            with pytest.raises(ConversationError):
                late.result(timeout=10)
        assert artifact(m3_engine, row, "generation") is None
        assert new.get_state(row.session_id).values == saved
        assert new.get_state(row.session_id).config["configurable"]["checkpoint_id"] == checkpoint
        assert result(new, fresh).answer == ANSWER
        assert len(search.calls) == 2 and saved["evidence_generation"] == 2
    finally:
        release.set()
        pool2.close()


def test_new_attempt_after_source_loss_has_distinct_evidence_generation(m3_engine, chat):
    graph, _, _, search = chat
    row = turn(m3_engine, session(m3_engine), "冒口有什么作用？")
    search.after = lambda: redact(m3_engine, search.sources[0]["document_id"])
    failed = invoke(graph, row)
    assert failed["terminal_status"] == "needs_recovery"
    search.after = None
    # No artifact was saved, but failed terminal checkpoint still blocks silent re-retrieval.
    assert invoke(graph, row) == failed and len(search.calls) == 1
    call(m3_engine, lambda r: r.transition_turn(row.session_id, row.id, expected_status="running",
         new_status="needs_recovery", expected_attempt=1, error_code="SYNTHETIC_RETRY"))
    fresh = call(m3_engine, lambda r: r.transition_turn(row.session_id, row.id, expected_status="needs_recovery",
         new_status="running", expected_attempt=1, rewrite_policy=current_rewrite_policy()))
    assert invoke(graph, fresh)["outcome"] == "answer"
    assert artifact(m3_engine, fresh, "retrieval").details["evidence_generation"] == 2
    assert [c.document_id for c in result(graph, fresh).citations] == [search.sources[1]["document_id"]]
    assert len(search.calls) == 2


# V2 graph/source-redaction protection is covered with real v2 source fixtures
# in test_graph_v2_postgresql.py; legacy graph cross-document aggregation is retired.


def test_source_lost_after_draft_commit_blocks_publication(m3_engine, chat):
    graph, _, _, search = chat
    row = turn(m3_engine, session(m3_engine), "冒口有什么作用？")
    invoke(graph, row)
    staged = result(graph, row)
    redact(m3_engine, search.sources[0]["document_id"])
    call(m3_engine, lambda r: r.transition_turn(row.session_id, row.id, expected_status="running",
         new_status="finalizing", expected_attempt=1))
    with pytest.raises(ConversationError):
        call(m3_engine, lambda r: r.publish_answer(row.session_id, row.id, staged.draft_snapshot_id, expected_attempt=1))
    assert call(m3_engine, lambda r: r.get_answer(row.session_id, row.id)) is None


def test_atomic_stage_failure_rolls_back_artifact_snapshots_and_log(m3_engine, chat, monkeypatch):
    graph, _, _, search = chat
    row = turn(m3_engine, session(m3_engine), "冒口有什么作用？")
    def failure(*args, **kwargs):
        raise RuntimeError("synthetic log failure after stage flush")
    monkeypatch.setattr(ConversationRepository, "record_retrieval", failure)
    with pytest.raises(RuntimeError, match="synthetic log failure"):
        invoke(graph, row)
    assert artifact(m3_engine, row, "retrieval") is None
    with factory(m3_engine)() as db:
        assert db.scalar(select(func.count()).select_from(QAEvidenceSnapshot).where(QAEvidenceSnapshot.turn_id == row.id)) == 0
        assert db.scalar(select(func.count()).select_from(RetrievalLog).where(RetrievalLog.turn_id == row.id)) == 0


def test_document_before_qa_lock_order_and_no_transactions_during_external_calls(m3_engine, chat):
    graph, _, provider, search = chat
    row = turn(m3_engine, session(m3_engine), "冒口有什么作用？")
    statements = []
    def begin(conn):
        batch = []
        statements.append(batch)
        conn.info["m3_lock_observation"] = batch
    def capture(conn, cursor, statement, parameters, context, many):
        if "FOR UPDATE" in statement.upper():
            conn.info["m3_lock_observation"].append(statement.lower())
    event.listen(m3_engine, "begin", begin)
    event.listen(m3_engine, "before_cursor_execute", capture)
    def check_connections(_=None):
        assert m3_engine.pool.checkedout() == 0
        return ANSWER
    provider.answer = check_connections
    search.before = check_connections
    try:
        assert invoke(graph, row)["outcome"] == "answer"
    finally:
        event.remove(m3_engine, "before_cursor_execute", capture)
        event.remove(m3_engine, "begin", begin)
    # Use each source-bearing transaction's FIRST QA lock as its ordering fence.
    source_transactions = [queries for queries in statements if any("from documents" in q for q in queries)]
    assert source_transactions
    for queries in source_transactions:
        assert next(i for i, q in enumerate(queries) if "from documents" in q) < next(i for i, q in enumerate(queries) if "from qa_sessions" in q)


def test_new_turn_resets_all_stage_refs(m3_engine, chat):
    graph, _, _, search = chat
    sid = session(m3_engine)
    row = turn(m3_engine, sid, "冒口有什么作用？")
    state = invoke(graph, row)
    publish_staged(m3_engine, graph, row)
    new = turn(m3_engine, sid, "冷铁有什么作用？")
    _, initial = graph._initial(sid, new.id, new.request_id, 1)
    assert all(initial[f"{s}_artifact_id"] is None for s in ("rewrite", *STAGE_KEYS))
    assert invoke(graph, new)["result_artifact_id"] != state["result_artifact_id"]
    assert len(search.calls) == 2


def test_checkpoint_size_is_bounded_and_business_history_retained(m3_engine, chat):
    graph, pool, provider, _ = chat
    sid = session(m3_engine)
    sizes = []
    # Repeat three bounded followups then explicitly switch/restate the topic.
    def response(request):
        if not request.messages[-1].content[0].text.startswith("{"):
            return "冒口 " + ANSWER
        parsed = [json.loads(m.content[0].text) for m in request.messages[1:-1]]
        if not parsed:
            return {"decision": "standalone", "standalone_query": "冒口有什么作用？"}
        message = next(m for m in reversed(parsed) if "冒口" in m["text"])
        return {"decision": "rewritten", "standalone_query": "冒口尺寸如何确定？", "history_scope": "recent",
                "referenced_message_ids": [message["message_id"]], "resolved_references": [
                    {"surface": "它", "referent": "冒口", "source_message_ids": [message["message_id"]]}]}
    provider.response = response
    for i in range(24):
        row = turn(m3_engine, sid, "冒口有什么作用？" if i % 4 == 0 else "它尺寸如何确定？")
        state = invoke(graph, row)
        assert state["outcome"] == "answer", state
        assert len(state["history_message_ids"]) <= 12
        sizes.append(len(json.dumps(state).encode()))
        publish_staged(m3_engine, graph, row)
    assert max(sizes) < 3000 and max(sizes[8:]) - min(sizes[8:]) < 600
    assert len(call(m3_engine, lambda r: r.list_messages(sid, limit=100))) == 48
    with pool.pool.connection() as conn:
        blobs = conn.execute("SELECT blob FROM checkpoint_blobs WHERE thread_id=%s UNION ALL SELECT blob FROM checkpoint_writes WHERE thread_id=%s", (str(sid), str(sid))).fetchall()
        assert blobs and all(b"M3_SYNTHETIC" not in (r["blob"] or b"") for r in blobs)
        checkpoints = list(pool.saver(sid, factory(m3_engine)).list({"configurable": {"thread_id": str(sid)}}))
        assert all(len(json.dumps(c.checkpoint["channel_values"]).encode()) < 4096 for c in checkpoints)


def test_independent_threads_generate_concurrently(m3_engine, chat):
    graph, _, provider, search = chat
    barrier = Barrier(2)
    def simultaneous(_):
        barrier.wait(timeout=10)
        return ANSWER
    provider.answer = simultaneous
    rows = [turn(m3_engine, session(m3_engine), "冒口有什么作用？") for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as workers:
        states = list(workers.map(lambda r: invoke(graph, r), rows))
    assert all(s["outcome"] == "answer" for s in states)
    assert len({s["result_artifact_id"] for s in states}) == 2
    assert len({s["rewrite_artifact_id"] for s in states}) == 2
    assert len(search.calls) == len(provider.answer_calls) == 2


def test_real_m3_lifespan_reuses_graph_service_and_reopens_checkpoint(m3_engine, monkeypatch):
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.postgres import PostgresSaver
    from app.db import session as session_module
    from app.llm import provider as provider_module
    from app.main import create_app
    from phase13_m2_support import settings_for
    install_search(monkeypatch, [source(m3_engine)])
    monkeypatch.setattr(session_module, "SessionLocal", factory(m3_engine))
    providers = []
    def build(_):
        provider = ChatProvider()
        providers.append(provider)
        return provider
    monkeypatch.setattr(provider_module, "build_llm_provider", build)
    monkeypatch.setattr(PostgresSaver, "setup", lambda *a, **kw: pytest.fail("Runtime DDL"))
    settings = settings_for(m3_engine, conversation_enabled=True, conversation_graph_version="phase13_m3_v2",
                            graph_retrieval_enabled=False, llm_provider="local", llm_model="synthetic")
    row = turn(m3_engine, session(m3_engine), "冒口有什么作用？")
    with TestClient(create_app(settings=settings)) as client:
        graph = client.app.state.conversation_graph
        assert graph.rag_nodes.evidence_service.graph_retrieval is client.app.state.graph_retrieval
        state = invoke(graph, row)
    assert providers[0].closed and graph.checkpoints.pool is None
    with TestClient(create_app(settings=settings)) as client:
        reopened = client.app.state.conversation_graph
        assert reopened.get_state(row.session_id).values == state
        assert result(reopened, row).checkpoint_complete
    assert providers[1].closed and not providers[1].calls
