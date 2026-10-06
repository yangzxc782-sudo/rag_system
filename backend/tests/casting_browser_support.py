"""Explicit isolated browser fixture; synthetic model/search, real engine and storage."""
import json
import os
from pathlib import Path
import socket

from phase13_m4_support import worker_engine


def casting_browser_worker(target, sources, directory, bucket, channel, stop):
    import uvicorn
    from sqlalchemy.orm import sessionmaker
    from app.db import session as session_module
    from app.llm import provider as provider_module
    from app.llm.messages import LLMMessage, LLMTextContentPart
    from app.local_server import server_config
    from app.main import create_app
    from app.services import hybrid_search
    from app.services.casting_engine import CastingEngine
    from phase13_m2_support import settings_for
    from phase13_m3_support import SearchHarness
    from phase13_integration.test_casting_answers_postgresql import AnswerProvider
    engine = worker_engine(target)
    session_module.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    provider = AnswerProvider()
    original = provider.generate

    def generate(request):
        if request.tools:
            ctx = json.loads(request.messages[-1].content[0].text)
            question = ctx["question"]
            if "第二" in question and ctx["recent_runs"]:
                provider.route = lambda _: LLMMessage("assistant", (LLMTextContentPart(json.dumps({
                    "route": "explain_existing", "source_run_id": ctx["recent_runs"][0]["run_id"], "candidate_rank": 2})),))
            else:
                provider.route = "rag" if "作用" in question else "calculate" if ctx["effective_input_file_id"] else "input_required"
        return original(request)

    provider.generate = generate
    provider_module.build_llm_provider = lambda _: provider
    hybrid_search.hybrid_search_chunks = SearchHarness(sources).hybrid
    execute = CastingEngine.execute

    def counted(self, *args, **kwargs):
        with (Path(directory) / "engine-calls.txt").open("a") as output:
            output.write("execute\n")
        return execute(self, *args, **kwargs)

    CastingEngine.execute = counted
    settings = settings_for(engine, conversation_enabled=True, casting_design_enabled=True,
        conversation_graph_version="casting_v1_v3", casting_work_root=Path(directory) / "runs",
        minio_endpoint=os.environ["CASTING_TEST_MINIO_ENDPOINT"], minio_root_user=os.environ["CASTING_TEST_MINIO_ACCESS"],
        minio_root_password=os.environ["CASTING_TEST_MINIO_SECRET"], minio_bucket=bucket,
        backend_cors_origins="http://127.0.0.1:3309", graph_retrieval_enabled=False, reranker_enabled=False,
        document_deletion_executor_enabled=False, llm_provider="local", llm_model="synthetic", llm_local_supports_tools=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 18005))

        class BrowserServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets)
                channel.send(18005 if self.started else None)

            async def on_tick(self, counter):
                return stop.is_set() or await super().on_tick(counter)
        try:
            BrowserServer(server_config(create_app(settings=settings), port=18005)).run(sockets=[sock])
        finally:
            engine.dispose()
