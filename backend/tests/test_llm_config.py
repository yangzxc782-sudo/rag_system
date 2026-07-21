from __future__ import annotations

import importlib
import logging
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.core.errors import LLM_CONFIG_INVALID, BusinessError
from app.core.config import Settings


def make_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "llm_provider": "local",
        "llm_temperature": 0.2,
        "llm_max_tokens": 2048,
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "local-model",
        "llm_api_key": "",
        "llm_timeout_seconds": 120,
        "llm_remote_base_url": "https://api.example.invalid/v1",
        "llm_remote_api_key": "test-key-not-real",
        "llm_remote_model": "remote-model",
        "llm_remote_timeout_seconds": 60,
        "llm_remote_supports_json_mode": False,
        "llm_remote_allow_insecure_http": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def configuration_module():
    return importlib.import_module("app.llm.configuration")


def test_remote_capability_and_insecure_http_defaults_are_conservative() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_remote_supports_json_mode is False
    assert settings.llm_remote_allow_insecure_http is False


def test_remote_json_capability_accepts_explicit_true() -> None:
    settings = Settings(
        _env_file=None,
        llm_remote_supports_json_mode=True,
    )

    assert settings.llm_remote_supports_json_mode is True


@pytest.mark.parametrize(
    ("configured_provider", "expected_provider", "expected_model"),
    [
        ("local", "local", "local-model"),
        ("api", "api", "remote-model"),
        ("openai_compatible", "local", "local-model"),
    ],
)
def test_resolve_active_llm_metadata_normalizes_provider_and_model(
    configured_provider: str,
    expected_provider: str,
    expected_model: str,
) -> None:
    module = configuration_module()

    metadata = module.resolve_active_llm_metadata(
        make_settings(llm_provider=configured_provider)
    )

    assert metadata.provider == expected_provider
    assert metadata.model == expected_model


def test_resolve_metadata_does_not_read_api_key_or_build_provider() -> None:
    module = configuration_module()

    class MetadataOnlySettings:
        llm_provider = "api"
        llm_remote_model = "remote-model"

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"unexpected settings access: {name}")

    metadata = module.resolve_active_llm_metadata(MetadataOnlySettings())

    assert metadata.provider == "api"
    assert metadata.model == "remote-model"


def test_unknown_provider_uses_dedicated_error_code() -> None:
    module = configuration_module()

    with pytest.raises(BusinessError) as exc_info:
        module.resolve_active_llm_metadata(make_settings(llm_provider="ollama"))

    assert exc_info.value.code == "LLM_PROVIDER_INVALID"
    assert exc_info.value.detail == {"field": "llm_provider"}


def test_local_validation_does_not_require_or_validate_remote_credentials() -> None:
    module = configuration_module()

    metadata = module.validate_active_llm_configuration(
        make_settings(
            llm_remote_base_url="",
            llm_remote_api_key="",
            llm_remote_model="",
            llm_remote_timeout_seconds=-1,
        )
    )

    assert metadata.provider == "local"


def test_api_validation_does_not_require_local_credentials() -> None:
    module = configuration_module()

    metadata = module.validate_active_llm_configuration(
        make_settings(
            llm_provider="api",
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            llm_timeout_seconds=-1,
        )
    )

    assert metadata.provider == "api"


def test_api_validation_uses_nonpersisting_secret_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = configuration_module()
    original_required_text = module._required_text
    checked_text_fields: list[str] = []

    def tracked_required_text(settings: object, field: str) -> str:
        checked_text_fields.append(field)
        if field == "llm_remote_api_key":
            raise AssertionError("remote key must use the secret-only validator")
        return original_required_text(settings, field)

    monkeypatch.setattr(module, "_required_text", tracked_required_text)
    secret_value = "test-validation-secret-not-real"
    settings = make_settings(
        llm_provider="api",
        llm_remote_api_key=SecretStr(secret_value),
    )

    metadata = module.validate_active_llm_configuration(settings)

    assert metadata.provider == "api"
    assert "llm_remote_api_key" not in checked_text_fields
    assert secret_value not in repr(metadata)
    assert all(value != secret_value for value in vars(module).values())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_remote_base_url", ""),
        ("llm_remote_api_key", ""),
        ("llm_remote_model", ""),
        ("llm_remote_timeout_seconds", 0),
    ],
)
def test_api_validation_rejects_missing_active_values(field: str, value: object) -> None:
    module = configuration_module()

    with pytest.raises(BusinessError) as exc_info:
        module.validate_active_llm_configuration(
            make_settings(llm_provider="api", **{field: value})
        )

    assert exc_info.value.code == LLM_CONFIG_INVALID
    assert exc_info.value.detail == {"field": field}


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.invalid/v1",
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000/v1",
    ],
)
def test_remote_url_allows_https_and_loopback_http(base_url: str) -> None:
    module = configuration_module()

    metadata = module.validate_active_llm_configuration(
        make_settings(llm_provider="api", llm_remote_base_url=base_url)
    )

    assert metadata.provider == "api"


@pytest.mark.parametrize(
    ("base_url", "reason"),
    [
        ("ftp://api.example.invalid/v1", "scheme"),
        ("https://user:password@api.example.invalid/v1", "userinfo"),
        ("https://api.example.invalid/v1?secret=value", "query"),
        ("https://api.example.invalid/v1#fragment", "fragment"),
        ("https:///v1", "host"),
        ("http://api.example.invalid/v1", "insecure_http"),
    ],
)
def test_remote_url_rejects_unsafe_forms(base_url: str, reason: str) -> None:
    module = configuration_module()

    with pytest.raises(BusinessError) as exc_info:
        module.validate_active_llm_configuration(
            make_settings(llm_provider="api", llm_remote_base_url=base_url)
        )

    assert exc_info.value.code == LLM_CONFIG_INVALID
    assert exc_info.value.detail == {
        "field": "llm_remote_base_url",
        "reason": reason,
    }
    assert "secret" not in str(exc_info.value.detail)
    assert "password" not in str(exc_info.value.detail)


def test_insecure_remote_http_requires_flag_and_warns_once(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = configuration_module()
    monkeypatch.setattr(module, "_insecure_http_warning_emitted", False)
    caplog.set_level(logging.WARNING, logger=module.__name__)
    settings = make_settings(
        llm_provider="api",
        llm_remote_base_url="http://api.example.invalid/v1",
        llm_remote_allow_insecure_http=True,
    )

    module.validate_active_llm_configuration(settings)
    module.validate_active_llm_configuration(settings)

    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "insecure_remote_transport_enabled"
    ]
    assert len(warnings) == 1
    assert getattr(warnings[0], "provider") == "api"
    assert getattr(warnings[0], "scheme") == "http"
    assert getattr(warnings[0], "hostname") == "api.example.invalid"
    assert "test-key-not-real" not in caplog.text


def test_legacy_alias_warns_once(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = configuration_module()
    monkeypatch.setattr(module, "_legacy_alias_warning_emitted", False)
    caplog.set_level(logging.WARNING, logger=module.__name__)
    settings = make_settings(llm_provider="openai_compatible")

    module.validate_active_llm_configuration(settings)
    module.validate_active_llm_configuration(settings)

    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "deprecated_llm_provider_alias"
    ]
    assert len(warnings) == 1
    assert getattr(warnings[0], "configured_provider") == "openai_compatible"
    assert getattr(warnings[0], "normalized_provider") == "local"
    assert "test-key-not-real" not in caplog.text
