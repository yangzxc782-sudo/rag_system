"""Synthetic M2 fixtures. New databases only on the verified M1 test instance."""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.langgraph import CheckpointPool
from app.llm.messages import LLMMessage, LLMTextContentPart
from app.llm.provider import LLMCapabilities, LLMGenerateResult, LLMUsage
from app.rag.conversation_graph import ConversationGraph
from app.rag.query_rewrite import QueryRewriter
from app.rag.query_rewrite_prompt import current_rewrite_policy
from app.schemas.conversation_persistence import SnapshotInput
from app.services.conversation_repository import ConversationRepository, fingerprint
from phase13_support import migration, verified_engine


M2_HEAD = "0010_phase13_checkpoints"


def database_engine(root, *, revision=M2_HEAD):
    # Recheck the root's URL and live cluster before *any* CREATE DATABASE.
    suffix = root.url.database.removeprefix("phase13_m1_test_")
    checked = verified_engine(root.url.render_as_string(hide_password=False),
                              f"phase13-m1-test-{suffix}", root.url.database)
    name = "phase13_m2_" + uuid4().hex
    try:
        with checked.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    finally:
        checked.dispose()
    engine = create_engine(root.url.set(database=name), pool_pre_ping=True)
    with engine.begin() as connection:
        assert tuple(connection.execute(text(
            "SELECT current_database(), current_user, current_setting('cluster_name')"
        )).one()) == (name, "phase13_m1", f"phase13-m1-test-{suffix}")
        migration(connection, revision)
    # Deliberately retained, no DROP DATABASE / schema cleanup.
    return engine


def settings_for(engine=None, **changes):
    changes.setdefault("conversation_graph_version", "phase13_m3_v2")
    return Settings(_env_file=None, database_url=(engine.url.render_as_string(hide_password=False)
                    if engine else "postgresql+psycopg://unused:unused@127.0.0.1:65000/unused"), **changes)


def factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def call(engine, operation):
    with factory(engine)() as db, db.begin():
        return operation(ConversationRepository(db))


def session(engine):
    return call(engine, lambda repo: repo.create_session(uuid4())).id


def turn(engine, sid, question):
    return call(engine, lambda repo: repo.start_turn(sid, uuid4(), question, rewrite_policy=current_rewrite_policy()))


def publish(engine, row, content="合成助手回答", outcome="no_context"):
    sid, tid, attempt = row.session_id, row.id, row.attempt_no
    aid = call(engine, lambda repo: repo.save_artifact(
        sid, tid, expected_attempt=attempt, key="synthetic_generation", kind="generation",
        input_fingerprint=fingerprint({"synthetic": True}),
    )).id
    snapshot = call(engine, lambda repo: repo.save_snapshots(
        sid, tid, aid, expected_attempt=attempt,
        snapshots=(SnapshotInput("answer", "answer_draft", {"text": content, "outcome": outcome}),),
    ))[0]
    call(engine, lambda repo: repo.transition_turn(sid, tid, expected_status="running",
         new_status="finalizing", expected_attempt=attempt))
    return call(engine, lambda repo: repo.publish_answer(sid, tid, snapshot.id, expected_attempt=attempt))


class FakeProvider:
    capabilities = LLMCapabilities(True, True, False, False, False)
    provider_name = "synthetic"

    def __init__(self, response=None):
        self.response, self.calls, self.closed = response, [], False

    def generate(self, request):
        self.calls.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        result = self.response(request) if callable(self.response) else self.response
        if result is None:
            raise AssertionError("Unexpected rewrite provider invocation")
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False)
        return LLMGenerateResult(LLMMessage("assistant", (LLMTextContentPart(result),)),
                                 "synthetic", "fake", LLMUsage(100, 30, 130))

    def close(self):
        self.closed = True


def riser_response(request):
    """Explicit synthetic responses for the integration corpus, not an NLP engine."""
    messages = [(m.role, json.loads(m.content[0].text)) for m in request.messages[1:]]
    current = messages[-1][1]["question"]
    pending = messages[-1][1].get("pending_clarification")
    history = messages[:-1]
    user = next((m for role, m in reversed(history) if role == "user"), None)
    queries = {
        "那它的尺寸呢？": "冒口的尺寸呢？",
        "它的尺寸呢？": "冒口的尺寸呢？",
        "它的尺寸是多少？": "冒口的尺寸是多少？",
        "它尺寸如何确定？": "冒口尺寸如何确定？",
        "它的尺寸如何确定？": "冒口的尺寸如何确定？",
        "它的尺寸怎么确定？": "冒口的尺寸怎么确定？",
        "那它尺寸怎么确定？": "冒口尺寸怎么确定？",
        "那它的尺寸应该怎么确定？": "冒口的尺寸应该怎么确定？",
        "有哪些限制条件？": "冒口有哪些限制条件？",
    }
    if pending and current == "冒口":
        return {"decision": "rewritten", "standalone_query": "冒口的尺寸怎么确定？",
                "history_scope": "clarification", "referenced_message_ids": [pending["message_id"]]}
    if current in queries:
        if user is None or user["text"] == "冒口和冷铁有什么区别？":
            return {"decision": "clarify", "clarification_reason": "请明确讨论的是冒口还是冷铁？",
                    "clarification_options": ["冒口", "冷铁"] if user else []}
        return {"decision": "rewritten", "standalone_query": queries[current], "history_scope": "recent",
                "referenced_message_ids": [user["message_id"]],
                "resolved_references": [{"surface": "", "referent": "冒口", "source_message_ids": [user["message_id"]]}]}
    return {"decision": "standalone", "standalone_query": current}


def empty_retrieval(query, limit, document_id):
    from types import SimpleNamespace
    from app.services.hybrid_search import HybridSearchResult
    return SimpleNamespace(outcome=SimpleNamespace(search_result=HybridSearchResult(query, limit, 0, []), applied=False,
        fallback_reason=None), hybrid_limit=limit, candidate_limit=None, hybrid_count=0,
        retrieval_ms=0, rerank_ms=0)


def runtime(engine, provider=None, **changes):
    settings = settings_for(engine, **changes)
    checkpoints = CheckpointPool(settings)
    checkpoints.open()
    provider = provider or FakeProvider(riser_response)
    graph = ConversationGraph(factory(engine), checkpoints, QueryRewriter(provider, settings), settings)
    # History/checkpoint tests use the current graph with a synthetic empty search.
    graph.rag_nodes.evidence_service.retrieve = empty_retrieval
    return graph, checkpoints, provider


def invoke(graph, row):
    return graph.invoke(row.session_id, row.id, row.request_id, row.attempt_no)
