"""Stage-5 publication/recovery on verified PostgreSQL and the actual Python engine."""
from copy import deepcopy
import json
from pathlib import Path
import os
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api.v1.conversations import router as chat_router
from app.api.v1.casting_design import router as casting_router
from app.core.errors import BusinessError
from app.core.config import Settings
from app.db.langgraph import CheckpointPool, CheckpointUnavailable
from app.local_server import LocalConversationTransport
from app.llm.messages import LLMMessage, LLMTextContentPart
from app.llm.provider import LLMGenerateResult
from app.models.casting_design_run import CastingDesignRun
from app.models.qa_evidence_snapshot import QAEvidenceSnapshot
from app.models.qa_turn import QATurn
from app.rag.casting_answer_nodes import SUMMARY_PROMPT
from app.rag.casting_answer_nodes import CastingAnswerNodes
from app.rag.casting_prompt import EXPLANATION_PROMPT
from app.schemas.conversation_persistence import SnapshotInput
from app.schemas.conversations import TurnCreateRequest
from app.services.conversation_repository import ConversationError, fingerprint
from app.services.conversations import Conversations
from app.services.casting_files import CastingFiles, CastingObjectStorage
from phase13_m2_support import call, database_engine, factory, session, settings_for
from phase13_m3_support import install_search, source
from phase13_support import migration
from .test_casting_graph_postgresql import RouterProvider, casting_db, runtime, check_checkpoints
from .test_casting_storage_postgresql import RAW, stack

pytestmark = [pytest.mark.phase13_integration, pytest.mark.casting_engine]


class AnswerProvider(RouterProvider):
    def __init__(self, route="calculate", summary=None):
        super().__init__(route)
        self.summary = summary
        self.summary_calls = []
        self.explanation_calls = []
        self.explanation = None

    def generate(self, request):
        if request.messages[0].content[0].text == EXPLANATION_PROMPT:
            self.explanation_calls.append(request)
            if isinstance(self.explanation, Exception):
                raise self.explanation
            ctx = json.loads(request.messages[-1].content[0].text)
            candidates = ctx["recommendation"]["candidates"]
            candidate = candidates[(ctx["candidate_rank"] or 1) - 1] if candidates else None
            body = self.explanation or (f"候选 {candidate['id']} 的参数来自已保存结果。" if candidate else "没有可行候选，请检查淘汰原因。")
            return LLMGenerateResult(LLMMessage("assistant", (LLMTextContentPart(body),)), "synthetic", "fake")
        if request.messages[0].content[0].text == SUMMARY_PROMPT:
            self.summary_calls.append(request)
            if isinstance(self.summary, Exception):
                raise self.summary
            facts = json.loads(request.messages[-1].content[0].text)["fact_catalog"]
            payload = self.summary if self.summary is not None else {"fact_refs": list(facts)}
            return LLMGenerateResult(LLMMessage("assistant", (LLMTextContentPart(json.dumps(payload)),)), "synthetic", "fake")
        return super().generate(request)


@pytest.fixture
def chat(casting_db, stack):
    service, sid = stack
    provider = AnswerProvider()
    graph, pool = runtime(casting_db, service, provider)
    app = Conversations(graph)
    yield app, provider, service, sid
    app.close()
    pool.close()


def request(service, sid, raw=RAW, question="请计算浇冒系统方案"):
    fid = service.files.upload(sid, uuid4(), "input.json", raw).file_id
    return TurnCreateRequest(request_id=uuid4(), question=question, casting_input_file_id=fid)


def test_http_compute_publication_replay_history_and_download(casting_db, chat, monkeypatch):
    app, provider, service, sid = chat
    api = FastAPI()
    api.include_router(chat_router, prefix="/api/v1")
    api.include_router(casting_router, prefix="/api/v1")
    api.state.settings, api.state.conversations, api.state.casting_design = app.settings, app, service
    client = TestClient(LocalConversationTransport(api), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345))
    uploaded = client.post(f"/api/v1/rag/sessions/{sid}/casting-inputs", data={"request_id": str(uuid4())}, files={"file": ("input.json", RAW)})
    assert uploaded.status_code == 200
    body = TurnCreateRequest(request_id=uuid4(), question="请计算浇冒系统方案", casting_input_file_id=uploaded.json()["data"]["file_id"])
    path = f"/api/v1/rag/sessions/{sid}/turns"
    response = client.post(path, json=body.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "completed" and data["outcome"] == "casting_design"
    result = data["result"]
    assert result["casting"]["summary_mode"] == "llm_fact_refs" and result["casting"]["candidate_count"] == 4
    assert "170 mm" in result["answer"] and "65.51%" in result["answer"]
    assert not result["citations"] and not result["sources"]
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("duplicate engine execution"))
    assert client.post(path, json=body.model_dump(mode="json")).json()["data"] == data
    assert app.request_status(sid, body.request_id).result.model_dump(mode="json") == result
    history = app.messages(sid).items
    assert len(history) == 2 and history[0].casting_input_file_id == body.casting_input_file_id
    assert history[0].casting_input_filename == "input.json"
    assert history[1].result.model_dump(mode="json") == result
    downloaded = client.get(f"/api/v1/rag/sessions/{sid}/casting-runs/{result['casting']['run_id']}/recommendation")
    assert downloaded.status_code == 200 and len(downloaded.json()["candidates"]) == 4
    assert len(provider.route_calls) == len(provider.summary_calls) == 1
    service.files.upload(sid, uuid4(), "not-selected.json", RAW)
    listed = service.files.list_inputs(sid, limit=1)
    assert listed.items[0].original_filename == "not-selected.json"
    assert listed.reusable_input.file_id == body.casting_input_file_id
    assert listed.reusable_input.original_filename == "input.json" and listed.reusable_input.admission_passed
    check_checkpoints(casting_db, sid)


@pytest.mark.parametrize("route", ["input_required", "clarify_selection"])
def test_source_free_engineering_guidance_completes_without_run(chat, route, monkeypatch):
    app, provider, service, sid = chat
    provider.route = route
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("guidance must not calculate"))
    _, result = app.submit(sid, TurnCreateRequest(request_id=uuid4(), question="请帮我设计"))
    assert result.status == "completed" and result.result.casting.result_status == route
    assert result.result.casting.run_id is None and len(provider.summary_calls) == 0


def test_no_feasible_candidate_is_published_without_new_scheme(chat):
    app, provider, service, sid = chat
    raw = (Path(__file__).parents[1] / "fixtures/casting/no_candidate.input.json").read_bytes()
    _, result = app.submit(sid, request(service, sid, raw))
    assert result.result.casting.result_status == "no_feasible_candidate"
    assert result.result.casting.recommended_candidate_id is None
    assert "没有方案通过筛选" in result.result.answer and "site-selection" in result.result.answer
    assert "直径" not in result.result.answer


@pytest.mark.parametrize("payload", [
    {"fact_refs": ["risers"], "diameter_mm": 999999},
    {"fact_refs": ["invented_candidate"]},
    {"fact_refs": ["checks"], "status": "passed"},
    {"fact_refs": ["candidate"], "recommended_candidate_id": "NEW"},
    {"fact_refs": ["candidate"], "text": "全部规则通过，可以生产。"},
], ids=["number", "foreign_ref", "status", "candidate", "free_text"])
def test_model_fact_substitution_falls_back_to_exact_template(chat, payload):
    app, provider, service, sid = chat
    provider.summary = payload
    _, result = app.submit(sid, request(service, sid))
    assert result.result.casting.summary_mode == "template"
    assert "999999" not in result.result.answer and "可以生产" not in result.result.answer
    assert "170 mm" in result.result.answer and "Pending" in result.result.answer


def test_explanation_timeout_retries_only_summary_and_keeps_frozen_rules(casting_db, chat, monkeypatch):
    app, provider, service, sid = chat
    req = request(service, sid)
    provider.summary = BusinessError("LLM_TIMEOUT", "synthetic timeout", status_code=504)
    with pytest.raises(BusinessError) as caught:
        app.submit(sid, req)
    assert caught.value.code == "LLM_TIMEOUT"
    status = app.request_status(sid, req.request_id)
    assert status.status == "failed" and status.can_retry
    with factory(casting_db)() as db:
        run = db.scalar(select(CastingDesignRun).where(CastingDesignRun.turn_id == status.turn_id))
        rid, rule_sha = run.id, run.rule_sha256
        assert run.status == "succeeded"
    service.files.upload(sid, uuid4(), "unused.json", RAW)
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("must reuse successful run"))
    monkeypatch.setattr(service.engine.rules, "select", lambda *a, **kw: pytest.fail("must not select new rules"))
    provider.summary = None
    _, done = app.submit(sid, req)
    assert done.status == "completed" and done.result.casting.run_id == rid and done.result.casting.rule_sha256 == rule_sha
    assert len(provider.route_calls) == 1 and len(provider.summary_calls) == 2
    with factory(casting_db)() as db:
        assert db.get(QATurn, done.turn_id).attempt_no == 2
    assert len(app.messages(sid).items) == 2


@pytest.mark.parametrize("window", ["after_terminal_checkpoint", "before_publish", "before_publication_commit", "after_publication_commit"])
def test_publication_interruptions_do_not_duplicate_messages_or_engine(chat, monkeypatch, window):
    app, provider, service, sid = chat
    req = request(service, sid)
    def interrupt(name, *args):
        if name == window:
            app.execution_hook = None
            raise RuntimeError("synthetic publication interruption")
    app.execution_hook = interrupt
    with pytest.raises(ConversationError):
        app.submit(sid, req)
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("must reuse run"))
    _, done = app.submit(sid, req)
    assert done.status == "completed" and len(app.messages(sid).items) == 2
    assert len(provider.route_calls) == len(provider.summary_calls) == 1


def test_historical_second_candidate_is_loaded_without_recalculation(chat, monkeypatch):
    app, provider, service, sid = chat
    _, first = app.submit(sid, request(service, sid))
    rid = first.result.casting.run_id
    raw, _ = service.recommendation(sid, rid)
    second = json.loads(raw)["candidates"][1]
    provider.route = lambda ctx: LLMMessage("assistant", (LLMTextContentPart(json.dumps({
        "route": "explain_existing", "source_run_id": str(rid), "candidate_rank": 2})),))
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("explanation cannot calculate"))
    _, explained = app.submit(sid, TurnCreateRequest(request_id=uuid4(), question="第二个候选的冒口尺寸是多少？"))
    assert explained.result.casting.run_id == rid and explained.result.casting.candidate_rank == 2
    assert second["id"] in explained.result.answer and "没有重新计算" in explained.result.answer
    assert explained.result.casting.recommended_candidate_id == first.result.casting.recommended_candidate_id
    assert explained.result.casting.summary_mode == "llm_explanation"
    ctx = json.loads(provider.explanation_calls[0].messages[-1].content[0].text)
    assert ctx["recommendation"] == json.loads(raw) and ctx["candidate_rank"] == 2
    assert ctx["input"] == json.loads(RAW)
    assert ctx["rules"] == json.loads(service.files.artifact_bytes(sid, rid, "rules.json"))
    assert app.messages(sid).items[-1].result == explained.result


def historical_route(provider, rid):
    provider.route = lambda _: LLMMessage("assistant", (LLMTextContentPart(json.dumps({
        "route": "explain_existing", "source_run_id": str(rid)})),))


def test_full_explanation_frozen_context_retry_and_publication_replay(casting_db, chat, monkeypatch):
    from app.services.casting_design import CastingDesignService
    from app.services.casting_repository import CastingRepository
    app, provider, service, sid = chat
    _, first = app.submit(sid, request(service, sid))
    rid = first.result.casting.run_id
    frozen = service.explanation_sources(sid, rid)
    # A newer uploaded file and unavailable current rules must not affect history.
    changed = json.loads(RAW)
    changed["hotspots"][0]["modulus_mm"] = 99
    fid = service.files.upload(sid, uuid4(), "new-input.json", json.dumps(changed).encode()).file_id
    historical_route(provider, rid)
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("history cannot calculate"))
    monkeypatch.setattr(service.engine.rules, "select", lambda *a, **kw: pytest.fail("history cannot select current rules"))
    monkeypatch.setattr(CastingDesignService, "prepare", lambda *a, **kw: pytest.fail("history cannot prepare computation"))
    monkeypatch.setattr(CastingRepository, "reserve_run", lambda *a, **kw: pytest.fail("history cannot create run"))
    provider.explanation = BusinessError("LLM_TIMEOUT", "synthetic timeout", status_code=504)
    question = "RS-01 冒口高度为什么是 220 mm？为什么不用 R160？"
    req = TurnCreateRequest(request_id=uuid4(), question=question, casting_input_file_id=fid)
    with pytest.raises(BusinessError) as exc:
        app.submit(sid, req)
    assert exc.value.code == "LLM_TIMEOUT"
    assert app.request_status(sid, req.request_id).can_retry
    provider.explanation = "要求模数：30 × 1.15 = 34.5 mm。R160 复核约 33.6 mm，不足；R170 为 35.619 mm。220 mm 来自 R170 目录高度。"
    _, result = app.submit(sid, req)
    assert result.result.casting.run_id == rid and result.result.casting.summary_mode == "llm_explanation"
    assert provider.explanation in result.result.answer
    for model_request in provider.explanation_calls:
        ctx = json.loads(model_request.messages[-1].content[0].text)
        assert {key: ctx[key] for key in frozen} == frozen
        assert ctx["question"] == question and model_request.tools == ()
    assert len(provider.explanation_calls) == 2
    assert app.submit(sid, req)[1].result == result.result
    assert app.request_status(sid, req.request_id).result == result.result
    assert app.messages(sid).items[-1].result == result.result
    assert len(provider.explanation_calls) == 2
    with factory(casting_db)() as db:
        assert len(list(db.scalars(select(CastingDesignRun).where(CastingDesignRun.session_id == sid)))) == 1
    check_checkpoints(casting_db, sid)


def test_explanation_budget_falls_back_explicitly_and_replays(chat, monkeypatch):
    from app.rag import casting_prompt
    app, provider, service, sid = chat
    _, first = app.submit(sid, request(service, sid))
    historical_route(provider, first.result.casting.run_id)
    monkeypatch.setattr(casting_prompt, "MAX_EXPLANATION_CONTEXT_BYTES", 10)
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("must not compute"))
    req = TurnCreateRequest(request_id=uuid4(), question="解释冒口高度")
    _, done = app.submit(sid, req)
    assert done.result.casting.summary_mode == "template" and not provider.explanation_calls
    assert casting_prompt.EXPLANATION_LIMIT_MESSAGE in done.result.answer
    assert "170 mm" in done.result.answer
    assert app.submit(sid, req)[1].result == done.result


def test_empty_candidates_explanation_receives_all_rejections(chat, monkeypatch):
    app, provider, service, sid = chat
    raw = (Path(__file__).parents[1] / "fixtures/casting/no_candidate.input.json").read_bytes()
    _, first = app.submit(sid, request(service, sid, raw))
    rid = first.result.casting.run_id
    historical_route(provider, rid)
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("must not compute"))
    _, done = app.submit(sid, TurnCreateRequest(request_id=uuid4(), question="为什么没有可行方案？"))
    data = json.loads(provider.explanation_calls[0].messages[-1].content[0].text)["recommendation"]
    assert data == json.loads(service.recommendation(sid, rid)[0]) and data["candidates"] == []
    assert data["rejected_attempts"] and done.result.casting.result_status == "no_feasible_candidate"


def test_missing_historical_snapshot_never_uses_current_rules(chat, monkeypatch):
    app, provider, service, sid = chat
    _, first = app.submit(sid, request(service, sid))
    historical_route(provider, first.result.casting.run_id)
    monkeypatch.setattr(service.engine.rules, "select", lambda *a, **kw: pytest.fail("no latest-rules fallback"))
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("must not compute"))
    # Remove only the synthetic in-memory object, never real object storage.
    key = next(key for key in service.files.storage.objects if key[1].endswith("/rules.json"))
    del service.files.storage.objects[key]
    with pytest.raises(BusinessError) as exc:
        app.submit(sid, TurnCreateRequest(request_id=uuid4(), question="解释历史方案"))
    assert exc.value.code == "CASTING_STORAGE_UNAVAILABLE" and not provider.explanation_calls


def test_explanation_text_hash_rejects_tampering_before_recovery(casting_db, chat, monkeypatch):
    app, provider, service, sid = chat
    _, first = app.submit(sid, request(service, sid))
    historical_route(provider, first.result.casting.run_id)
    original = CastingAnswerNodes.generate_casting_answer
    def interrupt(self, state, config):
        original(self, state, config)
        raise RuntimeError("interrupt after explanation commit")
    monkeypatch.setattr(CastingAnswerNodes, "generate_casting_answer", interrupt)
    req = TurnCreateRequest(request_id=uuid4(), question="解释历史方案")
    with pytest.raises(ConversationError):
        app.submit(sid, req)
    tid = app.request_status(sid, req.request_id).turn_id
    with factory(casting_db)() as db, db.begin():
        row = db.scalar(select(QAEvidenceSnapshot).where(QAEvidenceSnapshot.turn_id == tid))
        value = deepcopy(row.payload)
        value["text"] = "tampered explanation"
        row.payload = value
        row.content_fingerprint = fingerprint({"kind": "answer_draft", "version": 3, "payload": value, "sources": []})
    monkeypatch.setattr(CastingAnswerNodes, "generate_casting_answer", original)
    with pytest.raises(ConversationError) as exc:
        app.submit(sid, req)
    assert exc.value.code == "CASTING_PROVENANCE_INVALID"
    assert len(provider.explanation_calls) == 1


def test_tampered_draft_after_proof_is_rejected_and_releases_session(casting_db, chat):
    app, provider, service, sid = chat
    def tamper(name, graph, lease):
        if name != "before_publish": return
        with lease.session_factory() as db, db.begin():
            row = db.scalar(select(QAEvidenceSnapshot).where(QAEvidenceSnapshot.session_id == sid, QAEvidenceSnapshot.kind == "answer_draft"))
            value = deepcopy(row.payload)
            value["text"] = "改造方案：直径 999999 mm，已通过全部验证。"
            row.payload = value
            # Even recomputing the snapshot hash cannot make a changed answer valid.
            row.content_fingerprint = fingerprint({"kind": "answer_draft", "version": 3, "payload": value, "sources": []})
    app.execution_hook = tamper
    req = request(service, sid)
    with pytest.raises(ConversationError) as caught:
        app.submit(sid, req)
    assert caught.value.code == "CASTING_PROVENANCE_INVALID"
    status = app.request_status(sid, req.request_id)
    assert status.status == "failed" and not status.can_retry
    assert app.session_detail(sid).active_request is None
    assert len(app.messages(sid).items) == 1


def test_plain_rag_source_requirement_remains_strict(casting_db):
    sid = session(casting_db)
    row = call(casting_db, lambda repo: repo.start_turn(sid, uuid4(), "普通问答"))
    artifact = call(casting_db, lambda repo: repo.save_artifact(sid, row.id, expected_attempt=1, key="test", kind="generation", input_fingerprint="a" * 64))
    with pytest.raises(ConversationError) as caught:
        call(casting_db, lambda repo: repo.save_snapshots(sid, row.id, artifact.id, expected_attempt=1,
            snapshots=(SnapshotInput("answer", "answer_draft", {"text": "没有文档来源", "outcome": "answer"}),)))
    assert caught.value.code == "QA_SOURCE_REQUIRED"


def test_long_engineering_answer_does_not_exhaust_next_rag_history(casting_db, chat, monkeypatch):
    app, provider, service, sid = chat
    data = json.loads((Path(__file__).parents[1] / "fixtures/casting/converted.input.json").read_bytes())
    data["main_wall_mm"] /= 10
    data["parameter_metadata"]["main_wall_mm"] = {"unit": "cm"}
    _, first = app.submit(sid, request(service, sid, json.dumps(data).encode()))
    assert len(first.result.answer.encode()) > app.settings.conversation_history_max_estimated_tokens
    provider.route = "rag"
    search = install_search(monkeypatch, [source(casting_db)])
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("RAG cannot calculate"))
    _, second = app.submit(sid, TurnCreateRequest(request_id=uuid4(), question="冒口有什么作用？"))
    assert second.status == "completed" and second.outcome == "answer" and second.result.casting is None
    assert second.result.citations and len(search.calls) == 1
    assert len(app.messages(sid).items) == 4
    assert "sprue_throat_area_mm2" not in provider.rewrite_calls[0].messages[-1].content[0].text


def test_admission_issues_publish_then_corrected_upload_can_compute(chat):
    app, provider, service, sid = chat
    data = json.loads(RAW)
    data["parameter_metadata"] = {"casting_mass_kg": {"status": "Proposed"}}
    _, failed = app.submit(sid, request(service, sid, json.dumps(data).encode()))
    assert failed.status == "completed" and failed.result.casting.result_status == "admission_failed"
    info = failed.result.casting
    assert info.error.category == "admission" and info.error.issues and info.result_file_id is None
    assert "casting_mass_kg" in failed.result.answer and not provider.summary_calls
    _, corrected = app.submit(sid, request(service, sid))
    assert corrected.result.casting.result_status == "success"
    assert corrected.result.casting.input_file_id != info.input_file_id


@pytest.mark.parametrize("method", ["generate_casting_answer", "stage_casting_result"])
def test_artifact_commit_before_checkpoint_reuses_summary_and_run(chat, monkeypatch, method):
    app, provider, service, sid = chat
    req = request(service, sid)
    original = getattr(CastingAnswerNodes, method)
    def interrupt(self, state, config):
        original(self, state, config)
        raise RuntimeError("synthetic interruption after engineering artifact commit")
    monkeypatch.setattr(CastingAnswerNodes, method, interrupt)
    with pytest.raises(ConversationError):
        app.submit(sid, req)
    monkeypatch.setattr(CastingAnswerNodes, method, original)
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("must reuse run"))
    _, done = app.submit(sid, req)
    assert done.status == "completed" and len(app.messages(sid).items) == 2
    assert len(provider.route_calls) == len(provider.summary_calls) == 1


def test_model_cannot_explain_another_sessions_run(casting_db, chat, monkeypatch):
    app, provider, service, sid = chat
    _, first = app.submit(sid, request(service, sid))
    other = session(casting_db)
    provider.route = lambda _: LLMMessage("assistant", (LLMTextContentPart(json.dumps({
        "route": "explain_existing", "source_run_id": str(first.result.casting.run_id)})),))
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("must not calculate"))
    req = TurnCreateRequest(request_id=uuid4(), question="解释这次计算结果")
    with pytest.raises(ConversationError) as caught:
        app.submit(other, req)
    assert caught.value.code == "CASTING_TOOL_CALL_INVALID"
    assert len(app.messages(other).items) == 1


def test_publication_requires_verified_proof(chat, monkeypatch):
    from dataclasses import replace
    from app.rag.conversation_graph import ConversationGraph
    app, provider, service, sid = chat
    original = ConversationGraph.get_result
    def missing_proof(self, *args, **kwargs):
        staged = original(self, *args, **kwargs)
        return replace(staged, proof=None)
    monkeypatch.setattr(ConversationGraph, "get_result", missing_proof)
    with pytest.raises(ConversationError) as caught:
        app.submit(sid, request(service, sid))
    assert caught.value.code == "CASTING_PROVENANCE_INVALID"
    assert len(app.messages(sid).items) == 1


def test_migration_preserves_existing_turn_and_gates_engineering_publication(phase13_root_engine):
    engine = database_engine(phase13_root_engine, revision="0011_casting_storage")
    try:
        sid = session(engine)
        row = call(engine, lambda repo: repo.start_turn(sid, uuid4(), "已有问答"))
        pool = CheckpointPool(settings_for(engine, conversation_enabled=True, casting_design_enabled=True,
            conversation_graph_version="casting_v1_v3"))
        with pytest.raises(CheckpointUnavailable, match="0012"):
            pool.open()
        with engine.begin() as connection:
            migration(connection, "0012_casting_answers")
        pool.open()
        pool.close()
        saved = call(engine, lambda repo: repo.get_turn(sid, row.id))
        assert saved.request_fingerprint == row.request_fingerprint and saved.question == row.question
        with engine.connect() as connection:
            definition = connection.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_qa_turns_outcome'"))
            assert "casting_design" in definition and "answer" in definition
    finally:
        engine.dispose()


def test_chat_publication_with_real_isolated_minio(casting_db, chat):
    endpoint = os.environ.get("CASTING_TEST_MINIO_ENDPOINT", "")
    if not endpoint:
        pytest.skip("Requires explicitly provisioned isolated MinIO")
    host, port = endpoint.split(":")
    assert host == "127.0.0.1" and int(port) not in {9000, 9001}
    access = os.environ["CASTING_TEST_MINIO_ACCESS"]
    assert access.startswith("castingtest")
    storage = CastingObjectStorage(Settings(_env_file=None, minio_endpoint=endpoint,
        minio_root_user=access, minio_root_password=os.environ["CASTING_TEST_MINIO_SECRET"]))
    app, provider, service, sid = chat
    bucket = "casting-test-" + uuid4().hex
    try:
        storage.client.make_bucket(bucket)  # Isolated test bucket retained for audit.
        service.files = CastingFiles(factory(casting_db), storage, bucket=bucket)
        _, done = app.submit(sid, request(service, sid))
        assert done.status == "completed" and done.result.casting.candidate_count == 4
        raw, sha = service.recommendation(sid, done.result.casting.run_id)
        assert sha == done.result.casting.result_sha256 and len(json.loads(raw)["candidates"]) == 4
        assert app.messages(sid).items[1].result == done.result
    finally:
        storage.close()
