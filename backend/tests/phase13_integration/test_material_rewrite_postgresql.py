"""Guarded real PostgreSQL history/checkpoints with synthetic external services."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models.qa_turn_artifact import QATurnArtifact
from app.rag.conversation_state import REWRITE_ARTIFACT_KEY
from app.schemas.query_rewrite import RewriteArtifactDetails
from phase13_m2_support import (
    FakeProvider,
    call,
    database_engine,
    factory,
    invoke,
    publish,
    riser_response,
    runtime as m2_runtime,
    session,
    turn,
)
from phase13_m3_support import ChatProvider, install_search, publish_staged, result, runtime, source


pytestmark = pytest.mark.phase13_integration


@pytest.fixture(scope="module")
def material_engine(phase13_root_engine):
    engine = database_engine(phase13_root_engine)
    try:
        yield engine
    finally:
        engine.dispose()


def saved_rewrite(engine, row):
    artifact = call(engine, lambda repo: repo.get_artifact_by_key(
        row.session_id, row.id, attempt_no=row.attempt_no, key=REWRITE_ARTIFACT_KEY,
    ))
    assert artifact is not None
    details = RewriteArtifactDetails.model_validate_json(json.dumps(artifact.details, ensure_ascii=False))
    return artifact, details


def artifact_ids(engine, row):
    with factory(engine)() as db:
        return dict(db.execute(select(QATurnArtifact.artifact_key, QATurnArtifact.id).where(
            QATurnArtifact.session_id == row.session_id,
            QATurnArtifact.turn_id == row.id,
            QATurnArtifact.attempt_no == row.attempt_no,
        )).all())


@pytest.mark.parametrize("material", ["WCB", "QT500-7"])
def test_material_followup_reaches_hybrid_after_restart_and_reuses_artifacts(
    material_engine, monkeypatch, material,
):
    engine = material_engine
    evidence = f"SYNTHETIC_MATERIAL_EVIDENCE {material}的力学性能与化学成分需要查阅材料标准。"
    search = install_search(monkeypatch, [source(engine, content=evidence)])
    sid = session(engine)
    first = turn(engine, sid, f"{material}的力学性能如何？")
    first_user = call(engine, lambda repo: repo.get_user_message(sid, first.id))
    graph, pool, provider = runtime(engine, ChatProvider(answer=f"{material}的力学性能见文本证据。[1]"))
    try:
        assert invoke(graph, first)["outcome"] == "answer"
        first_assistant = publish_staged(engine, graph, first)
        assert call(engine, lambda repo: repo.get_turn(sid, first.id)).status == "completed"
        assert len(provider.rewrite_calls) == 1 and len(provider.answer_calls) == 1
    finally:
        pool.close()

    # A new graph and PostgreSQL pool must recover only published business history.
    graph2, pool2, provider2 = runtime(engine, ChatProvider(answer=f"{material}的成分需要依据当前材料标准。[1]",
        rewrite=lambda request: {"decision": "rewritten", "standalone_query": f"{material}的化学成分包括哪些元素？",
            "history_scope": "recent", "referenced_message_ids": [str(first_user.id)], "resolved_references": []}))
    try:
        current = turn(engine, sid, "它的成分含有什么？")
        state = invoke(graph2, current)
        assert state["terminal_status"] == "result_staged" and state["outcome"] == "answer"
        assert state["history_message_ids"] == [str(first_user.id), str(first_assistant.id)]
        artifact, details = saved_rewrite(engine, current)
        assert str(artifact.id) == state["rewrite_artifact_id"]
        assert details.history_message_ids == [first_user.id, first_assistant.id]
        assert details.result.decision == "rewritten"
        assert details.result.standalone_query == f"{material}的化学成分包括哪些元素？"
        assert details.result.referenced_message_ids == [first_user.id]
        assert details.result.resolved_references == []
        assert len(provider2.rewrite_calls) == 1 and len(provider2.answer_calls) == 1
        assert [request["query"] for request in search.calls] == [first.question, details.result.standalone_query]
        assert search.embedding.encode_query_calls == [first.question, details.result.standalone_query]
        assert result(graph2, current).checkpoint_complete

        before = artifact_ids(engine, current)
        assert len(before) == 6
        assert invoke(graph2, current) == state
        assert artifact_ids(engine, current) == before
        assert len(search.calls) == 2 and len(provider2.answer_calls) == 1
    finally:
        pool2.close()

    graph3, pool3, provider3 = runtime(engine)
    try:
        assert graph3.get_state(sid).values == state
        assert result(graph3, current).checkpoint_complete
        assert saved_rewrite(engine, current)[0].id == artifact.id
        assert artifact_ids(engine, current) == before
        publish_staged(engine, graph3, current)
        messages = call(engine, lambda repo: repo.list_messages(sid))
        assert [message.sequence_no for message in messages] == [1, 2, 3, 4]
        assert [message.role for message in messages] == ["user", "assistant", "user", "assistant"]
        assert messages[2].content == "它的成分含有什么？"
        assert call(engine, lambda repo: repo.get_turn(sid, current.id)).status == "completed"
        assert not provider3.calls
    finally:
        pool3.close()


@pytest.mark.parametrize("response", ["{invalid-json", '{"decision":"rewritten","standalone_query":42}'])
def test_rewrite_failure_is_technical_and_terminal_not_a_clarification(material_engine, response):
    engine = material_engine
    sid = session(engine)
    first = turn(engine, sid, "冒口有什么作用？")
    publish(engine, first, "冒口用于补缩。")
    current = turn(engine, sid, "它的尺寸如何确定？")
    graph, pool, provider = m2_runtime(engine, FakeProvider(response))
    try:
        state = invoke(graph, current)
        assert state["terminal_status"] == "needs_recovery"
        assert state["error_code"] == "QA_REWRITE_OUTPUT_INVALID" and state["outcome"] is None
        assert call(engine, lambda repo: repo.get_artifact_by_key(sid, current.id, attempt_no=1, key=REWRITE_ARTIFACT_KEY)) is None
        assert call(engine, lambda repo: repo.get_answer(sid, current.id)) is None
        assert len(provider.calls) == 1
    finally:
        pool.close()
    restarted, new_pool, new_provider = m2_runtime(engine)
    try:
        assert restarted.get_state(sid).values == state
        assert invoke(restarted, current) == state
        assert not new_provider.calls
    finally:
        new_pool.close()
