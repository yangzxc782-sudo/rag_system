"""Spawn-safe synthetic workers. Every process rechecks the isolated root."""
import os
from pathlib import Path
import re
from uuid import UUID

from sqlalchemy import create_engine, text

from app.db.conversation_lock import ThreadExecutionLocks
from app.schemas.conversations import TurnCreateRequest
from app.services.conversations import Conversations
from phase13_m2_support import settings_for
from phase13_m3_support import ChatProvider, SearchHarness, runtime
from phase13_support import verified_engine


def worker_engine(target):
    url, cluster, root_name, database = target
    root = verified_engine(url, cluster, root_name)
    try:
        if not re.fullmatch(r"phase13_m2_[a-f0-9]{32}", database):
            raise ValueError("Not a synthetic Phase 13 worker database")
        engine = create_engine(root.url.set(database=database), pool_pre_ping=True)
        with engine.connect() as db:
            assert tuple(db.execute(text("SELECT current_database(), current_user, current_setting('cluster_name')")).one()) == (
                database, "phase13_m1", cluster)
        return engine
    finally:
        root.dispose()


def worker_target(root, engine):
    return (root.url.render_as_string(hide_password=False),
            "phase13-m1-test-" + root.url.database.removeprefix("phase13_m1_test_"),
            root.url.database, engine.url.database)


def lock_worker(target, sid, channel, release):
    engine = worker_engine(target)
    locks = ThreadExecutionLocks(settings_for(engine))
    try:
        with locks.acquire(UUID(sid)) as lease:
            channel.send((bool(lease), lease.backend_pid if lease else None))
            if lease:
                assert release.wait(30)
    finally:
        locks.close()
        engine.dispose()


def conversation_worker(target, sid, request, sources, crash_at, calls_path, channel):
    engine = worker_engine(target)
    from app.services import hybrid_search
    search = SearchHarness(sources)
    def record(name):
        with Path(calls_path).open("a", encoding="utf-8") as output:
            output.write(name + "\n")
    search.before = lambda: record("hybrid")
    hybrid_search.hybrid_search_chunks = search.hybrid
    provider = ChatProvider()
    original_generate = provider.generate
    def generate(request):
        record("rewrite" if request.messages[-1].content[0].text.startswith("{") else "llm")
        return original_generate(request)
    provider.generate = generate
    graph, pool, _ = runtime(engine, provider=provider)
    def crash():
        channel.send("crashed:" + crash_at)
        os._exit(73)  # Only this dedicated synthetic child, no cleanup handlers.
    def hook(event, bound, lease):
        if event == "before_graph" and crash_at in {"artifact", "draft"}:
            def after_stage(kind):
                if kind == ("retrieval" if crash_at == "artifact" else "generation"):
                    crash()
            bound.rag_nodes.after_stage_commit = after_stage
        elif event == crash_at:
            crash()
    service = Conversations(graph, execution_hook=hook)
    try:
        status, result = service.submit(UUID(sid), TurnCreateRequest.model_validate(request))
        channel.send((status, result.model_dump(mode="json")))
    finally:
        service.close()
        pool.close()
        engine.dispose()


def http_worker(target, sources, channel, stop):
    """Actual loopback Uvicorn/FastAPI lifespan, real PG, synthetic externals."""
    import socket
    import uvicorn
    from sqlalchemy.orm import sessionmaker
    from app.db import session as session_module
    from app.llm import provider as provider_module
    from app.services import hybrid_search
    from app.local_server import server_config
    from app.main import create_app
    engine = worker_engine(target)
    session_module.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    provider_module.build_llm_provider = lambda _: ChatProvider()
    harness = SearchHarness(sources)
    hybrid_search.hybrid_search_chunks = harness.hybrid
    settings = settings_for(engine, conversation_enabled=True, conversation_graph_version="phase13_m3_v2",
                            graph_retrieval_enabled=False, reranker_enabled=False,
                            llm_provider="local", llm_model="synthetic")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        class TestServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets)
                channel.send(sock.getsockname()[1] if self.started else None)
            async def on_tick(self, counter):
                return stop.is_set() or await super().on_tick(counter)
        try:
            TestServer(server_config(create_app(settings=settings), port=0)).run(sockets=[sock])
        finally:
            engine.dispose()
