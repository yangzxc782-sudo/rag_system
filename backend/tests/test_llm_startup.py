from __future__ import annotations

import builtins
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.errors import (
    LLM_CONFIG_INVALID,
    LLM_PROVIDER_INVALID,
    BusinessError,
)
from app.llm.provider import clear_llm_provider_cache


def minimal_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "llm_provider": "local",
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "test-local-model",
        "llm_api_key": "",
        "llm_timeout_seconds": 1,
        "llm_temperature": 0.0,
        "llm_max_tokens": 32,
        "llm_remote_base_url": "",
        "llm_remote_api_key": None,
        "llm_remote_model": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def clear_caches_between_tests():
    clear_llm_provider_cache()
    get_settings.cache_clear()
    yield
    clear_llm_provider_cache()
    get_settings.cache_clear()


def test_create_app_accepts_explicit_minimal_local_settings_without_remote_key() -> None:
    from app.main import create_app

    app = create_app(settings=minimal_settings())

    assert app.title == "铸型工艺知识库 RAG 系统"


def test_create_app_rejects_unknown_provider_during_startup() -> None:
    from app.main import create_app

    with pytest.raises(BusinessError) as exc_info:
        create_app(settings=minimal_settings(llm_provider="unknown"))

    assert exc_info.value.code == LLM_PROVIDER_INVALID


def test_create_app_rejects_api_without_remote_key_during_startup() -> None:
    from app.main import create_app

    with pytest.raises(BusinessError) as exc_info:
        create_app(
            settings=minimal_settings(
                llm_provider="api",
                llm_remote_base_url="https://api.example.invalid/v1",
                llm_remote_model="remote-model",
                llm_remote_api_key=None,
            )
        )

    assert exc_info.value.code == LLM_CONFIG_INVALID
    assert exc_info.value.detail == {"field": "llm_remote_api_key"}


def test_create_app_validates_before_importing_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    events: list[str] = []
    original_validate = main_module.validate_active_llm_configuration
    original_import = builtins.__import__

    def tracked_validate(settings: Settings):
        events.append("validate")
        return original_validate(settings)

    def tracked_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ):
        if name == "app.api.v1.router":
            events.append("router")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(main_module, "validate_active_llm_configuration", tracked_validate)
    monkeypatch.setattr(builtins, "__import__", tracked_import)

    main_module.create_app(settings=minimal_settings())

    assert events[:2] == ["validate", "router"]


def test_startup_validation_does_not_build_provider_or_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.llm.provider as provider_module
    from app.main import create_app

    def unexpected_build(*args: object, **kwargs: object) -> object:
        raise AssertionError("startup must not build an LLM provider")

    monkeypatch.setattr(provider_module, "build_llm_provider", unexpected_build)

    app = create_app(settings=minimal_settings())

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_clean_settings_source_does_not_require_backend_env_or_remote_key() -> None:
    from app.main import create_app

    settings = minimal_settings()
    assert settings.llm_remote_api_key is None

    app = create_app(settings=settings)

    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_valid_api_configuration_can_start_without_constructing_api_adapter() -> None:
    from app.main import create_app

    app = create_app(
        settings=minimal_settings(
            llm_provider="api",
            llm_remote_base_url="https://api.example.invalid/v1",
            llm_remote_api_key="test-key-not-real",
            llm_remote_model="remote-model",
        )
    )

    assert app.title == "铸型工艺知识库 RAG 系统"


def test_shutdown_clears_cached_provider_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.llm.provider as provider_module
    from app.main import create_app

    fake_provider = SimpleNamespace(close_calls=0)

    def close() -> None:
        fake_provider.close_calls += 1

    fake_provider.close = close
    monkeypatch.setattr(provider_module, "get_settings", lambda: minimal_settings())
    monkeypatch.setattr(
        provider_module,
        "build_llm_provider",
        lambda settings, *, client=None: fake_provider,
    )
    assert provider_module.get_llm_provider() is fake_provider

    app = create_app(settings=minimal_settings())
    with TestClient(app):
        pass

    assert fake_provider.close_calls == 1
    provider_module.clear_llm_provider_cache()
    assert fake_provider.close_calls == 1


def test_deletion_executor_disabled_does_not_construct_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    def unexpected_executor(*args: object, **kwargs: object) -> object:
        raise AssertionError("disabled startup must not construct executor")

    monkeypatch.setattr(main_module, "DocumentDeletionExecutor", unexpected_executor)
    app = main_module.create_app(
        settings=minimal_settings(document_deletion_executor_enabled=False)
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert not hasattr(app.state, "document_deletion_executor")


def test_deletion_executor_enabled_starts_once_and_stops_before_llm_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    events: list[str] = []

    class FakeExecutor:
        def __init__(self, *, settings: Settings) -> None:
            assert settings.document_deletion_executor_enabled is True
            events.append("construct")

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

        def join(self, timeout: float | None = None) -> None:
            assert timeout == 10
            events.append("join")

    monkeypatch.setattr(main_module, "DocumentDeletionExecutor", FakeExecutor)
    monkeypatch.setattr(
        main_module,
        "clear_llm_provider_cache",
        lambda: events.append("llm-cleanup"),
    )
    app = main_module.create_app(
        settings=minimal_settings(document_deletion_executor_enabled=True)
    )

    with TestClient(app) as client:
        assert events == ["construct", "start"]
        assert client.get("/health").status_code == 200

    assert events == ["construct", "start", "stop", "join", "llm-cleanup"]


def test_repeated_enabled_lifespans_stop_each_executor_without_worker_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    events: list[tuple[int, str]] = []
    executors: list[FakeExecutor] = []

    class FakeExecutor:
        def __init__(self, *, settings: Settings) -> None:
            assert settings.document_deletion_executor_enabled is True
            self.instance_id = len(executors) + 1
            self.alive = False
            self.joined = False
            executors.append(self)
            events.append((self.instance_id, "construct"))

        def start(self) -> None:
            self.alive = True
            events.append((self.instance_id, "start"))

        def stop(self) -> None:
            self.alive = False
            events.append((self.instance_id, "stop"))

        def join(self, timeout: float | None = None) -> None:
            assert timeout == 10
            assert self.alive is False
            self.joined = True
            events.append((self.instance_id, "join"))

    monkeypatch.setattr(main_module, "DocumentDeletionExecutor", FakeExecutor)
    monkeypatch.setattr(
        main_module,
        "clear_llm_provider_cache",
        lambda: events.append((len(executors), "llm-cleanup")),
    )

    for expected_instance_id in (1, 2):
        app = main_module.create_app(
            settings=minimal_settings(document_deletion_executor_enabled=True)
        )
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert executors[-1].instance_id == expected_instance_id
            assert executors[-1].alive is True
        assert executors[-1].alive is False
        assert executors[-1].joined is True

    assert executors[0] is not executors[1]
    assert events == [
        (1, "construct"),
        (1, "start"),
        (1, "stop"),
        (1, "join"),
        (1, "llm-cleanup"),
        (2, "construct"),
        (2, "start"),
        (2, "stop"),
        (2, "join"),
        (2, "llm-cleanup"),
    ]


@pytest.mark.parametrize("failure", [None, "body", "start"])
def test_pdf_drain_precedes_all_dependency_cleanup(monkeypatch, failure):
    import app.main as main_module
    from app.services import conversations, casting_files, casting_engine, casting_design
    events = []
    @contextmanager
    def runtime(*a):
        events.append("runtime-open")
        try:
            yield SimpleNamespace(rag_nodes=True, session_factory=object())
        finally:
            events.append("runtime-close")
    class PDFExecutor:
        def __init__(self, **kw): pass
        def start(self):
            events.append("pdf-start")
            if failure == "start":
                raise ValueError("synthetic startup failure")
        async def shutdown(self):
            events.append("pdf-stop")
            await asyncio.sleep(0)
            assert not any(event.endswith("close") for event in events)
            events.append("pdf-drained")
    class DeletionExecutor:
        def __init__(self, **kw): pass
        def start(self): pass
        def stop(self): events.append("delete-stop")
        def join(self, timeout): events.append("delete-joined")
    monkeypatch.setattr(main_module, "_conversation_runtime", runtime)
    monkeypatch.setattr(main_module, "DocumentProcessingExecutor", PDFExecutor)
    monkeypatch.setattr(main_module, "DocumentDeletionExecutor", DeletionExecutor)
    monkeypatch.setattr(main_module, "Neo4jRepository", lambda *a: SimpleNamespace(close=lambda: events.append("graph-close")))
    monkeypatch.setattr(main_module, "GraphRetrievalService", lambda *a, **kw: object())
    monkeypatch.setattr(conversations, "Conversations", lambda *a: SimpleNamespace(close=lambda: events.append("service-close")))
    monkeypatch.setattr(casting_files, "CastingObjectStorage", lambda *a: SimpleNamespace(close=lambda: events.append("casting-close")))
    monkeypatch.setattr(casting_files, "CastingFiles", lambda *a, **kw: object())
    monkeypatch.setattr(casting_engine, "CastingEngine", lambda **kw: object())
    monkeypatch.setattr(casting_design, "CastingDesignService", lambda *a, **kw: object())
    monkeypatch.setattr(main_module, "clear_llm_provider_cache", lambda: events.append("llm-close"))
    monkeypatch.setattr(main_module, "close_reranking_service", lambda: events.append("reranker-close"))
    settings = minimal_settings(conversation_enabled=True, casting_design_enabled=True,
        document_processing_executor_enabled=True, document_deletion_executor_enabled=True)
    async def exercise():
        async with main_module._lifespan(settings)(SimpleNamespace(state=SimpleNamespace())):
            if failure == "body":
                raise ValueError("synthetic business failure")
    if failure:
        with pytest.raises(ValueError, match="synthetic"):
            asyncio.run(exercise())
    else:
        asyncio.run(exercise())
    assert events == ["runtime-open", "pdf-start", "delete-stop", "pdf-stop", "pdf-drained",
        "delete-joined", "casting-close", "service-close", "runtime-close", "llm-close", "reranker-close", "graph-close"]
