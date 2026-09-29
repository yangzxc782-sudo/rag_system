"""Loopback browser E2E fixture; real M4 HTTP/lifespan/PG, synthetic externals."""
from pathlib import Path
import time

from phase13_m4_support import worker_engine


def browser_http_worker(target, sources, empty_document, port, origin, calls_path, channel, stop):
    import socket
    import uvicorn
    from sqlalchemy.orm import sessionmaker
    from app.db import session as session_module
    from app.llm import provider as provider_module
    from app.services import hybrid_search
    from app.local_server import server_config
    from app.main import create_app
    from phase13_m2_support import settings_for
    from phase13_m3_support import ANSWER, ChatProvider, SearchHarness

    engine = worker_engine(target)  # Revalidate independent root, role, cluster and child DB.
    session_module.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    harness, empty = SearchHarness(sources), SearchHarness([])

    def record(value):
        with Path(calls_path).open("a", encoding="utf-8") as output:
            output.write(value + "\n")

    def hybrid(db, **kwargs):
        record("hybrid:" + kwargs["query"])
        chosen = empty if str(kwargs.get("document_id")) == empty_document else harness
        return chosen.hybrid(db, **kwargs)

    hybrid_search.hybrid_search_chunks = hybrid

    def respond(request):
        record("answer")
        if "慢速工艺测试" in request.messages[-1].content[0].text:
            time.sleep(3)  # Synthetic slow provider, only this foreground request.
        return ANSWER

    provider_module.build_llm_provider = lambda _: ChatProvider(answer=respond)
    settings = settings_for(engine, conversation_enabled=True, conversation_graph_version="phase13_m3_v2",
        graph_retrieval_enabled=False, reranker_enabled=False, document_deletion_executor_enabled=False,
        backend_cors_origins=origin, llm_provider="local", llm_model="synthetic")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))

        class BrowserServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets)
                channel.send(port if self.started else None)

            async def on_tick(self, counter):
                return stop.is_set() or await super().on_tick(counter)

        try:
            BrowserServer(server_config(create_app(settings=settings), port=port)).run(sockets=[sock])
        finally:
            engine.dispose()
