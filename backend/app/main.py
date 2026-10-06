from contextlib import asynccontextmanager, contextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.graph.repository import Neo4jRepository
from app.llm.configuration import validate_active_llm_configuration
from app.llm.provider import clear_llm_provider_cache
from app.services.graph_retrieval import GraphRetrievalService
from app.services.reranking import close_reranking_service
from app.tasks.document_deletion_executor import DocumentDeletionExecutor


@contextmanager
def _conversation_runtime(settings: Settings, graph_retrieval=None):
    if not bool(getattr(settings, "conversation_enabled", False)):
        yield None
        return
    from sqlalchemy.engine import make_url

    from app.db.langgraph import CheckpointPool, CheckpointUnavailable
    from app.db.session import SessionLocal
    from app.llm.provider import build_llm_provider
    from app.rag.conversation_graph import ConversationGraph
    from app.rag.query_rewrite import QueryRewriter

    # Both resources must address the same database. Explicit create_app settings
    # must never pair a test checkpoint pool with the default business Session.
    if SessionLocal.kw["bind"].url != make_url(settings.database_url):
        raise CheckpointUnavailable("QA_CHECKPOINT_DATABASE_MISMATCH")
    checkpoints = CheckpointPool(settings)
    provider = None
    try:
        checkpoints.open()  # SELECT-only readiness; schema is managed by Alembic.
        provider = build_llm_provider(settings)
        yield ConversationGraph(SessionLocal, checkpoints, QueryRewriter(provider, settings), settings,
                                graph_retrieval=graph_retrieval)
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            checkpoints.close()


def _lifespan(settings: Settings):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        graph_repository = Neo4jRepository(settings)
        app.state.graph_repository = graph_repository
        app.state.graph_retrieval = GraphRetrievalService(settings, repository=graph_repository)
        executor: DocumentDeletionExecutor | None = None
        try:
            with _conversation_runtime(settings, app.state.graph_retrieval) as conversation:
                app.state.conversation_graph = conversation
                from app.services.conversations import Conversations
                service = Conversations(conversation) if conversation is not None and conversation.rag_nodes is not None else None
                app.state.conversations = service
                casting_storage = None
                app.state.casting_design = None
                try:
                    if settings.casting_design_enabled:
                        import os
                        from app.core.config import BACKEND_DIR
                        from app.services.casting_design import CastingDesignService
                        from app.services.casting_engine import CastingEngine
                        from app.services.casting_files import CastingFiles, CastingObjectStorage
                        casting_storage = CastingObjectStorage(settings)
                        files = CastingFiles(conversation.session_factory, casting_storage, bucket=settings.minio_bucket)
                        python = settings.casting_python_executable or BACKEND_DIR / ".venv-casting" / (
                            "Scripts/python.exe" if os.name == "nt" else "bin/python")
                        app.state.casting_design = CastingDesignService(files,
                            CastingEngine(python_executable=python, work_root=settings.casting_work_root),
                            project_key=settings.casting_project_key)
                        conversation.casting_service = app.state.casting_design
                    if bool(getattr(settings, "document_deletion_executor_enabled", False)):
                        executor = DocumentDeletionExecutor(settings=settings)
                        app.state.document_deletion_executor = executor
                        executor.start()
                    yield
                finally:
                    try:
                        try:
                            if casting_storage is not None:
                                casting_storage.close()
                        finally:
                            app.state.casting_design = None
                            if service is not None:
                                service.close()
                    finally:
                        app.state.conversations = None
                        app.state.conversation_graph = None
        finally:
            try:
                if executor is not None:
                    executor.stop()
                    executor.join(
                        timeout=float(
                            settings.document_deletion_shutdown_grace_seconds
                        )
                    )
            finally:
                try:
                    clear_llm_provider_cache()
                finally:
                    try:
                        close_reranking_service()
                    finally:
                        graph_repository.close()

    return lifespan


def create_app(*, settings: Settings | None = None) -> FastAPI:
    configure_logging()
    active_settings = settings or get_settings()
    validate_active_llm_configuration(active_settings)

    from app.api.v1.router import api_router

    app = FastAPI(title=active_settings.app_name, lifespan=_lifespan(active_settings))
    app.state.settings = active_settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.backend_cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix=active_settings.api_v1_prefix)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": active_settings.app_name}

    return app


app = create_app()
