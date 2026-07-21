from __future__ import annotations

import builtins
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
