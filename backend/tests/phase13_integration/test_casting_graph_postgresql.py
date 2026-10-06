"""Real PostgreSQL checkpoints + ToolNode + engine, synthetic routing provider."""
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.db.conversation_lock import ThreadExecutionLocks
from app.db.langgraph import CheckpointPool
from app.llm.messages import LLMFunctionCall, LLMMessage, LLMTextContentPart, LLMToolCall
from app.llm.provider import LLMCapabilities, LLMGenerateResult
from app.models.casting_design_run import CastingDesignRun
from app.models.qa_turn_artifact import QATurnArtifact
from app.rag.casting_nodes import CastingNodes
from app.rag.casting_prompt import TOOL_NAME
from app.rag.casting_projection import project_result, projection_bytes
from app.rag.conversation_graph import ConversationGraph
from app.rag.conversation_state import CASTING_GRAPH_VERSION
from app.rag.query_rewrite import QueryRewriter
from app.rag.query_rewrite_prompt import current_rewrite_policy
from app.schemas.casting_graph import ROUTE_KEY, TOOL_KEY
from app.schemas.conversations import TurnCreateRequest
from app.services.conversation_repository import ConversationError, ConversationRepository
from app.services.conversations import Conversations
from phase13_m2_support import call, database_engine, factory, session, settings_for
from phase13_m3_support import ChatProvider, install_search, source
from .test_casting_storage_postgresql import RAW, stack, finish_turn

pytestmark = pytest.mark.phase13_integration


@pytest.fixture(scope="module")
def casting_db(phase13_root_engine):
    engine = database_engine(phase13_root_engine, revision="0012_casting_answers")
    yield engine
    engine.dispose()


class RouterProvider(ChatProvider):
    capabilities = LLMCapabilities(True, True, True, False, False)
    def __init__(self, route="calculate"):
        super().__init__()
        self.route, self.route_calls = route, []
    def generate(self, request):
        from app.rag.casting_answer_nodes import SUMMARY_PROMPT
        if request.messages[0].content[0].text == SUMMARY_PROMPT:
            facts = json.loads(request.messages[-1].content[0].text)["fact_catalog"]
            return LLMGenerateResult(LLMMessage("assistant", (LLMTextContentPart(json.dumps({"fact_refs": list(facts)})),)), "synthetic", "fake")
        if not request.tools:
            return super().generate(request)
        self.route_calls.append(request)
        ctx = json.loads(request.messages[-1].content[0].text)
        if callable(self.route):
            msg = self.route(ctx)
        elif self.route == "calculate":
            msg = LLMMessage("assistant", tool_calls=(LLMToolCall("native_call_1",
                LLMFunctionCall(TOOL_NAME, json.dumps({"input_file_id": ctx["effective_input_file_id"]}))),))
        else:
            msg = LLMMessage("assistant", (LLMTextContentPart(json.dumps({"route": self.route})),))
        return LLMGenerateResult(msg, "synthetic", "fake")


def accept(db, sid, *, fid=None, version=CASTING_GRAPH_VERSION, question="请计算浇冒系统方案"):
    return call(db, lambda repo: repo.start_turn(sid, uuid4(), question,
        rewrite_policy=current_rewrite_policy(), casting_enabled=True, casting_input_file_id=fid, graph_version=version))


def runtime(db, service, provider, *, version=CASTING_GRAPH_VERSION):
    settings = settings_for(db, conversation_enabled=True, casting_design_enabled=True,
                            conversation_graph_version=version)
    pool = CheckpointPool(settings)
    pool.open()
    graph = ConversationGraph(factory(db), pool, QueryRewriter(provider, settings), settings, casting_service=service)
    return graph, pool


def invoke(graph, row):
    return graph.invoke(row.session_id, row.id, row.request_id, row.attempt_no)


def check_checkpoints(db, sid):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    serializer = JsonPlusSerializer()
    with db.connect() as connection:
        checkpoints = connection.execute(text("SELECT checkpoint FROM langgraph_checkpoints.checkpoints WHERE thread_id=:sid"), {"sid": str(sid)}).scalars().all()
        blobs = connection.execute(text("SELECT type, blob FROM langgraph_checkpoints.checkpoint_blobs WHERE thread_id=:sid AND blob IS NOT NULL"), {"sid": str(sid)}).all()
        writes = connection.execute(text("SELECT type, blob FROM langgraph_checkpoints.checkpoint_writes WHERE thread_id=:sid"), {"sid": str(sid)}).all()
    assert checkpoints
    forbidden = ("recommendation.json", "ToolMessage", "AIMessage", "casting_mass_kg", "risers", "tool_calls", "current_message\"")
    serialized = json.dumps(checkpoints, ensure_ascii=False)
    for type_, blob in [*blobs, *writes]:
        value = serializer.loads_typed((type_, bytes(blob)))
        serialized += repr(value)
    for word in forbidden:
        assert word not in serialized, word


@pytest.mark.casting_engine
def test_native_toolnode_one_execution_replay_and_bound_lease(casting_db, stack, monkeypatch):
    service, sid = stack
    fid = service.files.upload(sid, uuid4(), "input.json", RAW).file_id
    row = accept(casting_db, sid, fid=fid)
    provider = RouterProvider()
    graph, pool = runtime(casting_db, service, provider)
    locks = ThreadExecutionLocks(graph.nodes.settings)
    try:
        with locks.acquire(sid) as lease:
            bound = graph.bind_execution(lease)
            state = invoke(bound, row)
            assert state["terminal_status"] == "result_staged", state
            result = bound.get_casting_result(sid, row.id, row.request_id, 1)
            assert result["projection"]["candidate_count"] == 4
            assert result["model_request"].tools == ()
            assert result["model_request"].messages[-1].role == "tool"
            assert result["input_reused"] is False
        monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("duplicate calculation"))
        assert invoke(graph, row)["source_run_id"] == state["source_run_id"]
        assert len(provider.route_calls) == 1
        check_checkpoints(casting_db, sid)
        with factory(casting_db)() as db:
            rows = list(db.scalars(select(CastingDesignRun).where(CastingDesignRun.turn_id == row.id)))
            assert len(rows) == 1
            assert db.scalar(select(QATurnArtifact).where(QATurnArtifact.turn_id == row.id,
                QATurnArtifact.artifact_key == TOOL_KEY)).details["run_id"] == str(rows[0].id)
    finally:
        locks.close()
        pool.close()


@pytest.mark.parametrize("attachment", [False, True])
def test_knowledge_questions_keep_rag_path_even_with_attachment(casting_db, stack, monkeypatch, attachment):
    service, sid = stack
    install_search(monkeypatch, [source(casting_db)])
    fid = service.files.upload(sid, uuid4(), "input.json", RAW).file_id if attachment else None
    row = accept(casting_db, sid, fid=fid, question="冒口有什么作用？")
    provider = RouterProvider("rag")
    graph, pool = runtime(casting_db, service, provider)
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("knowledge question must not calculate"))
    try:
        result = invoke(graph, row)
        assert result["terminal_status"] == "result_staged" and result["outcome"] == "answer", result
        assert graph.get_result(sid, row.id, row.request_id, 1).checkpoint_complete
        assert len(provider.route_calls) == 1 and len(provider.answer_calls) == 1
        assert result["tool_artifact_id"] is None
        check_checkpoints(casting_db, sid)
    finally:
        pool.close()


def test_missing_input_only_handoff_and_no_engine(casting_db, stack, monkeypatch):
    service, sid = stack
    row = accept(casting_db, sid)
    graph, pool = runtime(casting_db, service, RouterProvider("input_required"))
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("missing input"))
    try:
        assert invoke(graph, row)["terminal_status"] == "result_staged"
        result = graph.get_casting_result(sid, row.id, row.request_id, 1)
        assert result["route"] == "input_required" and "JSON" in result["message"]
        assert call(casting_db, lambda repo: repo.get_answer(sid, row.id)) is None
    finally:
        pool.close()


@pytest.mark.parametrize("case", ["wrong_id", "extra", "unknown", "multiple"])
def test_invalid_model_tool_never_reaches_engine(casting_db, stack, monkeypatch, case):
    service, sid = stack
    fid = service.files.upload(sid, uuid4(), "input.json", RAW).file_id
    row = accept(casting_db, sid, fid=fid)
    def response(ctx):
        args = {"input_file_id": str(uuid4()) if case == "wrong_id" else str(fid)}
        if case == "extra": args["session_id"] = str(sid)
        call = LLMToolCall("test_call", LLMFunctionCall("shell" if case == "unknown" else TOOL_NAME, json.dumps(args)))
        return LLMMessage("assistant", tool_calls=(call, call) if case == "multiple" else (call,))
    graph, pool = runtime(casting_db, service, RouterProvider(response))
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("invalid call executed"))
    try:
        result = invoke(graph, row)
        assert result["error_code"] == "CASTING_TOOL_CALL_INVALID" and result["terminal_status"] == "needs_recovery"
        with factory(casting_db)() as db:
            assert db.scalar(select(CastingDesignRun).where(CastingDesignRun.turn_id == row.id)) is None
    finally:
        pool.close()


def test_v2_turn_keeps_graph_and_fingerprint_under_v3_configuration(casting_db, stack, monkeypatch):
    service, sid = stack
    install_search(monkeypatch, [source(casting_db)])
    row = accept(casting_db, sid, version="phase13_m3_v2", question="冒口有什么作用？")
    provider = RouterProvider(lambda _: pytest.fail("v2 must not route through casting"))
    graph, pool = runtime(casting_db, service, provider)
    chat = Conversations(graph)
    try:
        status, result = chat.submit(sid, TurnCreateRequest(request_id=row.request_id, question=row.question, limit=8))
        assert status == 200 and result.status == "completed"
        state = graph.get_state(sid).values
        assert state["graph_version"] == "phase13_m3_v2" and state["input_fingerprint"] == row.request_fingerprint
        assert "route_artifact_id" not in state
    finally:
        chat.close()
        pool.close()


def test_v3_then_new_v2_turn_clears_old_casting_channels(casting_db, stack, monkeypatch):
    service, sid = stack
    install_search(monkeypatch, [source(casting_db)])
    row = accept(casting_db, sid, question="冒口有什么作用？")
    graph, pool = runtime(casting_db, service, RouterProvider("rag"))
    try:
        assert invoke(graph, row)["terminal_status"] == "result_staged"
        finish_turn(casting_db, row)
        old = accept(casting_db, sid, version="phase13_m3_v2", question="冒口有什么作用？")
        assert invoke(graph, old)["graph_version"] == "phase13_m3_v2"
        assert graph.get_state(sid).values["graph_version"] == "phase13_m3_v2"
    finally:
        pool.close()


@pytest.mark.casting_engine
def test_after_tool_metadata_commit_before_checkpoint_reuses_run(casting_db, stack, monkeypatch):
    service, sid = stack
    fid = service.files.upload(sid, uuid4(), "input.json", RAW).file_id
    row = accept(casting_db, sid, fid=fid)
    graph, pool = runtime(casting_db, service, RouterProvider())
    original = CastingNodes._save
    def fail(self, identity, key, details, parent=None):
        result = original(self, identity, key, details, parent)
        if key == TOOL_KEY:
            raise RuntimeError("injected interruption after artifact commit")
        return result
    try:
        monkeypatch.setattr(CastingNodes, "_save", fail)
        with pytest.raises(RuntimeError):
            invoke(graph, row)
        monkeypatch.setattr(CastingNodes, "_save", original)
        monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("must reuse run"))
        result = graph.resume(sid, row.id, row.request_id, 1)
        assert result["terminal_status"] == "result_staged"
        assert graph.get_casting_result(sid, row.id, row.request_id, 1)["projection"]["candidate_count"] == 4
        check_checkpoints(casting_db, sid)
    finally:
        pool.close()


@pytest.mark.casting_engine
def test_real_no_candidate_tool_result_is_a_business_result(casting_db, stack):
    service, sid = stack
    raw = (Path(__file__).parents[1] / "fixtures/casting/no_candidate.input.json").read_bytes()
    fid = service.files.upload(sid, uuid4(), "input.json", raw).file_id
    row = accept(casting_db, sid, fid=fid)
    graph, pool = runtime(casting_db, service, RouterProvider())
    try:
        assert invoke(graph, row)["terminal_status"] == "result_staged"
        result = graph.get_casting_result(sid, row.id, row.request_id, 1)["projection"]
        assert result["status"] == "no_feasible_candidate" and result["candidate_count"] == 0
        assert result["recommended_candidate"] is None and result["rejected_summary"]
        check_checkpoints(casting_db, sid)
    finally:
        pool.close()


@pytest.mark.casting_engine
def test_history_selection_is_frozen_and_text_edits_block_mistaken_model_call(casting_db, stack, monkeypatch):
    service, sid = stack
    fid = service.files.upload(sid, uuid4(), "input.json", RAW).file_id
    row = accept(casting_db, sid, fid=fid)
    provider = RouterProvider()
    graph, pool = runtime(casting_db, service, provider)
    try:
        assert invoke(graph, row)["terminal_status"] == "result_staged"
        finish_turn(casting_db, row)
        followup = accept(casting_db, sid, question="用之前的输入重新计算一次。")
        # Uploading another file cannot alter this turn's frozen selection.
        service.files.upload(sid, uuid4(), "unused.json", RAW)
        assert invoke(graph, followup)["terminal_status"] == "result_staged"
        result = graph.get_casting_result(sid, followup.id, followup.request_id, 1)
        assert result["input_reused"] is True and result["projection"]["input_file_id"] == str(fid)
        finish_turn(casting_db, followup)
        edited = accept(casting_db, sid, question="把质量改为 500 kg，再重新计算。")
        monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("must not calculate old parameters"))
        state = invoke(graph, edited)
        assert state["casting_route"] == "input_required" and state["tool_artifact_id"] is None
        with factory(casting_db)() as db:
            assert db.scalar(select(CastingDesignRun).where(CastingDesignRun.turn_id == edited.id)) is None
    finally:
        pool.close()


def test_disabling_feature_cannot_overwrite_a_saved_v3_execution(casting_db, stack):
    service, sid = stack
    row = accept(casting_db, sid)
    graph, pool = runtime(casting_db, service, RouterProvider("input_required"))
    try:
        invoke(graph, row)
        settings = graph.nodes.settings.model_copy(update={"casting_design_enabled": False, "conversation_graph_version": "phase13_m3_v2"})
        disabled = ConversationGraph(factory(casting_db), pool, graph.nodes.rewriter, settings)
        with pytest.raises(ConversationError) as caught:
            invoke(disabled, row)
        assert caught.value.code == "QA_GRAPH_VERSION_MISMATCH"
        assert graph.get_state(sid).values["graph_version"] == CASTING_GRAPH_VERSION
    finally:
        pool.close()
