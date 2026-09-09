from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.graph.repository import Neo4jRepository
from app.llm.configuration import validate_active_llm_configuration
from app.llm.provider import clear_llm_provider_cache
from app.tasks.document_deletion_executor import DocumentDeletionExecutor


def _lifespan(settings: Settings):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        graph_repository = Neo4jRepository(settings)
        app.state.graph_repository = graph_repository
        executor: DocumentDeletionExecutor | None = None
        if bool(getattr(settings, "document_deletion_executor_enabled", False)):
            executor = DocumentDeletionExecutor(settings=settings)
            app.state.document_deletion_executor = executor
            executor.start()
        try:
            yield
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
