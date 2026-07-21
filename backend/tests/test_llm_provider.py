from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib
import inspect
import json
from threading import Lock
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import OpenAI

from app.core.errors import (
    LLM_CONFIG_INVALID,
    LLM_GENERATION_FAILED,
    LLM_PARAMETER_UNSUPPORTED,
    LLM_PROVIDER_INVALID,
    LLM_RESPONSE_INVALID,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    BusinessError,
)
from app.llm import (
    LLMFunctionTool,
    LLMGenerateRequest,
    LLMGenerateResult,
    LLMImageURLContentPart,
    LLMMessage,
    LLMTextContentPart,
)


class FakeMessage:
    def __init__(self, content: str | None, *, tool_calls: list[object] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, content: str | None, *, tool_calls: list[object] | None = None) -> None:
        self.message = FakeMessage(content, tool_calls=tool_calls)


class FakeUsage:
    prompt_tokens = 12
    completion_tokens = 5
    total_tokens = 17


class FakeCompletion:
    def __init__(
        self,
        content: str | None = "测试回答",
        *,
        choices: list[object] | None = None,
        tool_calls: list[object] | None = None,
    ) -> None:
        self.id = "chatcmpl-local-test"
        self.choices = choices if choices is not None else [
            FakeChoice(content, tool_calls=tool_calls)
        ]
        self.usage = FakeUsage()


class FakeCompletions:
    def __init__(self, response: object | None = None, exc: Exception | None = None) -> None:
        self.response = response or FakeCompletion()
        self.exc = exc
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.response


class FakeChat:
    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions


class FakeOpenAIClient:
    def __init__(self, completions: FakeCompletions | None = None) -> None:
        self.completions = completions or FakeCompletions()
        self.chat = FakeChat(self.completions)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class APIConnectionError(Exception):
    pass


class APITimeoutError(Exception):
    pass


class APIStatusError(Exception):
    pass


def make_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "llm_provider": "local",
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "qwen3:8b",
        "llm_api_key": "",
        "llm_temperature": 0.2,
        "llm_max_tokens": 2048,
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


def local_module():
    return importlib.import_module("app.llm.local")


def provider_module():
    return importlib.import_module("app.llm.provider")


def message(role: str, text: str) -> LLMMessage:
    return LLMMessage(
        role=role,  # type: ignore[arg-type]
        content=(LLMTextContentPart(text=text),),
    )


def make_mock_transport_openai_client(
    captured_bodies: list[dict[str, object]],
) -> OpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"x-request-id": "req-m1b-test"},
            json={
                "id": "chatcmpl-m1b-test",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen3:8b",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"items":[]}'},
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                },
            },
        )

    return OpenAI(
        api_key="test-key-not-real",
        base_url="https://example.invalid/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture(autouse=True)
def clear_provider_cache_between_tests():
    module = provider_module()
    clear = getattr(module, "clear_llm_provider_cache", None)
    if clear is not None:
        clear()
    yield
    if clear is not None:
        clear()


def test_llm_package_import_does_not_create_real_client() -> None:
    import app.llm as llm

    assert callable(llm.get_llm_provider)


@pytest.mark.parametrize("configured_provider", ["local", "openai_compatible"])
def test_build_llm_provider_returns_normalized_local_provider(
    configured_provider: str,
) -> None:
    module = provider_module()

    provider = module.build_llm_provider(
        make_settings(llm_provider=configured_provider),
        client=FakeOpenAIClient(),
    )

    assert provider.__class__.__name__ == "LocalLLMProvider"
    assert provider.provider_name == "local"
    assert provider.capabilities.supports_json_mode is True
    assert provider.capabilities.supports_think is True
    assert provider.capabilities.supports_tools is False
    assert provider.capabilities.supports_parallel_tool_calls is False
    assert provider.capabilities.supports_image_input is False


def test_legacy_get_with_settings_remains_available_until_m3() -> None:
    module = provider_module()

    provider = module.get_llm_provider(make_settings(), client=FakeOpenAIClient())

    assert provider.provider_name == "local"


def test_legacy_get_with_settings_stays_isolated_from_global_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = provider_module()
    legacy_settings = make_settings(llm_model="legacy-model")
    cached_settings = make_settings(llm_model="cached-model")
    providers: list[SimpleNamespace] = []

    def fake_build(settings: object, *, client: object | None = None) -> object:
        provider = SimpleNamespace(settings=settings, close_calls=0)

        def close() -> None:
            provider.close_calls += 1

        provider.close = close
        providers.append(provider)
        return provider

    monkeypatch.setattr(module, "get_settings", lambda: cached_settings)
    monkeypatch.setattr(module, "build_llm_provider", fake_build)

    first_legacy = module.get_llm_provider(legacy_settings)
    second_legacy = module.get_llm_provider(legacy_settings)

    assert first_legacy is not second_legacy
    assert module._provider_cache is None

    first_cached = module.get_llm_provider()

    assert first_cached is module._provider_cache
    assert first_cached is not first_legacy
    assert first_cached is not second_legacy
    assert module.get_llm_provider() is first_cached

    module.clear_llm_provider_cache()

    assert module._provider_cache is None
    assert first_cached.close_calls == 1
    # Legacy providers are deliberately outside the formal cache lifecycle until M3.
    assert first_legacy.close_calls == 0
    assert second_legacy.close_calls == 0

    second_cached = module.get_llm_provider()

    assert second_cached is not first_cached
    assert second_cached is module._provider_cache
    assert first_legacy.close_calls == 0
    assert second_legacy.close_calls == 0


def test_api_provider_before_m2_is_valid_but_unavailable_without_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.llm.configuration import (
        resolve_active_llm_metadata,
        validate_active_llm_configuration,
    )

    module = provider_module()
    remote_url = "https://remote.example.invalid/v1"
    remote_key = "test-remote-key-not-real"
    remote_model = "remote-test-model"

    class APISentinelSettings:
        llm_provider = "api"
        llm_temperature = 0.2
        llm_max_tokens = 2048
        llm_remote_base_url = remote_url
        llm_remote_api_key = remote_key
        llm_remote_model = remote_model
        llm_remote_timeout_seconds = 60
        llm_remote_supports_json_mode = False
        llm_remote_allow_insecure_http = False

        def __getattribute__(self, name: str) -> object:
            if name in {"llm_base_url", "llm_model", "llm_api_key"}:
                raise AssertionError(f"API path read local setting: {name}")
            return object.__getattribute__(self, name)

    settings = APISentinelSettings()
    local_constructions: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fail_if_local_is_constructed(
        *args: object, **kwargs: object
    ) -> object:
        local_constructions.append((args, kwargs))
        raise AssertionError("API path must not construct LocalLLMProvider")

    monkeypatch.setattr(
        local_module(),
        "LocalLLMProvider",
        fail_if_local_is_constructed,
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)

    assert resolve_active_llm_metadata(settings).provider == "api"
    assert resolve_active_llm_metadata(settings).model == remote_model
    assert validate_active_llm_configuration(settings).provider == "api"
    assert validate_active_llm_configuration(settings).model == remote_model

    def assert_m2_stage_error(call: object) -> None:
        with pytest.raises(BusinessError) as exc_info:
            call()  # type: ignore[operator]

        error = exc_info.value
        assert error.code == LLM_CONFIG_INVALID
        assert error.code != LLM_PROVIDER_INVALID
        assert error.status_code == 400
        assert error.detail == {"field": "llm_provider"}
        rendered_error = f"{error.message} {error.detail!r}"
        for sensitive_value in (
            remote_url,
            remote_key,
            remote_model,
            "Authorization",
            "headers",
            "prompt",
        ):
            assert sensitive_value not in rendered_error

    assert_m2_stage_error(lambda: module.build_llm_provider(settings))
    assert module._provider_cache is None
    assert_m2_stage_error(lambda: module.get_llm_provider(settings))
    assert module._provider_cache is None
    assert_m2_stage_error(module.get_llm_provider)
    assert module._provider_cache is None
    assert local_constructions == []


def test_factory_defensively_revalidates_configuration() -> None:
    module = provider_module()

    with pytest.raises(BusinessError) as exc_info:
        module.build_llm_provider(make_settings(llm_model=""))

    assert exc_info.value.code == LLM_CONFIG_INVALID
    assert exc_info.value.detail == {"field": "llm_model"}


def test_local_provider_does_not_create_client_before_generate() -> None:
    created: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeOpenAIClient:
        created.append(kwargs)
        return FakeOpenAIClient()

    local_module().LocalLLMProvider(make_settings(), client_factory=factory)

    assert created == []


def test_local_provider_uses_nonsecret_placeholder_and_disables_sdk_retries() -> None:
    created: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeOpenAIClient:
        created.append(kwargs)
        return FakeOpenAIClient()

    provider = local_module().LocalLLMProvider(make_settings(), client_factory=factory)
    provider.generate(LLMGenerateRequest.from_prompt("问题"))

    assert created == [
        {
            "base_url": "http://localhost:11434/v1",
            "api_key": "ollama",
            "timeout": 120,
            "max_retries": 0,
        }
    ]


def test_repeated_generation_reuses_one_sdk_client() -> None:
    created: list[FakeOpenAIClient] = []

    def factory(**kwargs: object) -> FakeOpenAIClient:
        client = FakeOpenAIClient()
        created.append(client)
        return client

    provider = local_module().LocalLLMProvider(make_settings(), client_factory=factory)

    provider.generate(LLMGenerateRequest.from_prompt("问题一"))
    provider.generate(LLMGenerateRequest.from_prompt("问题二"))

    assert len(created) == 1
    assert len(created[0].completions.calls) == 2


def test_local_generate_preserves_four_turn_text_message_order() -> None:
    fake_client = FakeOpenAIClient()
    provider = local_module().LocalLLMProvider(make_settings(), client=fake_client)
    request = LLMGenerateRequest(
        messages=(
            message("system", "system rules"),
            message("user", "turn 1"),
            message("assistant", "answer 1"),
            message("user", "turn 2"),
        ),
        temperature=0.3,
        max_tokens=512,
        timeout_seconds=9,
        stop=("END",),
    )

    result = provider.generate(request)

    assert result.text == "测试回答"
    assert result.provider == "local"
    assert result.model == "qwen3:8b"
    assert fake_client.completions.calls == [
        {
            "model": "qwen3:8b",
            "messages": [
                {"role": "system", "content": "system rules"},
                {"role": "user", "content": "turn 1"},
                {"role": "assistant", "content": "answer 1"},
                {"role": "user", "content": "turn 2"},
            ],
            "temperature": 0.3,
            "max_tokens": 512,
            "timeout": 9,
            "stop": ["END"],
        }
    ]


def test_generate_passes_optional_json_mode_and_think_parameters() -> None:
    fake_client = FakeOpenAIClient()
    provider = local_module().LocalLLMProvider(make_settings(), client=fake_client)

    provider.generate(
        LLMGenerateRequest.from_prompt(
            '{"items":[]}',
            json_mode=True,
            think=False,
        )
    )

    assert fake_client.completions.calls[0]["response_format"] == {
        "type": "json_object"
    }
    assert fake_client.completions.calls[0]["extra_body"] == {"think": False}


def test_openai_sdk_merges_json_mode_and_think_into_wire_body() -> None:
    captured_bodies: list[dict[str, object]] = []
    client = make_mock_transport_openai_client(captured_bodies)
    provider = local_module().LocalLLMProvider(make_settings(), client=client)

    try:
        provider.generate(
            LLMGenerateRequest.from_prompt(
                '{"items":[]}',
                json_mode=True,
                think=False,
            )
        )
    finally:
        provider.close()

    assert captured_bodies[0]["think"] is False
    assert captured_bodies[0]["response_format"] == {"type": "json_object"}


def test_openai_sdk_does_not_send_think_when_request_omits_it() -> None:
    captured_bodies: list[dict[str, object]] = []
    client = make_mock_transport_openai_client(captured_bodies)
    provider = local_module().LocalLLMProvider(make_settings(), client=client)

    try:
        provider.generate(LLMGenerateRequest.from_prompt("plain request"))
    finally:
        provider.close()

    assert "think" not in captured_bodies[0]


def test_tool_request_is_rejected_before_client_call() -> None:
    fake_client = FakeOpenAIClient()
    provider = local_module().LocalLLMProvider(make_settings(), client=fake_client)
    request = LLMGenerateRequest(
        messages=(message("user", "use a tool"),),
        tools=(
            LLMFunctionTool(
                name="lookup",
                parameters={"type": "object", "properties": {}},
            ),
        ),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(request)

    assert exc_info.value.code == LLM_PARAMETER_UNSUPPORTED
    assert exc_info.value.detail == {
        "provider": "local",
        "parameter": "tools",
        "required_capability": "supports_tools",
    }
    assert fake_client.completions.calls == []
    assert "lookup" not in str(exc_info.value.detail)


def test_image_request_is_rejected_before_client_call() -> None:
    fake_client = FakeOpenAIClient()
    provider = local_module().LocalLLMProvider(make_settings(), client=fake_client)
    request = LLMGenerateRequest(
        messages=(
            LLMMessage(
                role="user",
                content=(
                    LLMTextContentPart(text="describe"),
                    LLMImageURLContentPart(
                        image_url="https://images.example.invalid/secret.png"
                    ),
                ),
            ),
        )
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(request)

    assert exc_info.value.code == LLM_PARAMETER_UNSUPPORTED
    assert exc_info.value.detail == {
        "provider": "local",
        "parameter": "image_input",
        "required_capability": "supports_image_input",
    }
    assert fake_client.completions.calls == []
    assert "secret.png" not in str(exc_info.value.detail)


def test_result_contains_assistant_message_usage_and_request_id() -> None:
    provider = local_module().LocalLLMProvider(
        make_settings(),
        client=FakeOpenAIClient(),
    )

    result = provider.generate(LLMGenerateRequest.from_prompt("问题"))

    assert result.message == message("assistant", "测试回答")
    assert result.usage is not None
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 5
    assert result.usage.total_tokens == 17
    assert result.request_id == "chatcmpl-local-test"


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (APIConnectionError("down"), LLM_UNAVAILABLE),
        (APITimeoutError("timeout"), LLM_TIMEOUT),
        (APIStatusError("bad response with secret"), LLM_GENERATION_FAILED),
    ],
)
def test_transport_maps_sdk_failures_without_exposing_exception_text(
    exc: Exception,
    expected_code: str,
) -> None:
    provider = local_module().LocalLLMProvider(
        make_settings(),
        client=FakeOpenAIClient(FakeCompletions(exc=exc)),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("问题"))

    assert exc_info.value.code == expected_code
    assert "secret" not in str(exc_info.value.detail)


@pytest.mark.parametrize(
    "completion",
    [
        FakeCompletion(choices=[]),
        FakeCompletion(content=None),
        FakeCompletion(content="", tool_calls=[object()]),
    ],
)
def test_invalid_local_response_maps_to_response_invalid(completion: object) -> None:
    provider = local_module().LocalLLMProvider(
        make_settings(),
        client=FakeOpenAIClient(FakeCompletions(response=completion)),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("问题"))

    assert exc_info.value.code == LLM_RESPONSE_INVALID


def test_result_constructor_no_longer_accepts_raw_parameter() -> None:
    assert "raw" not in inspect.signature(LLMGenerateResult).parameters


def test_provider_and_transport_close_are_idempotent() -> None:
    client = FakeOpenAIClient()
    provider = local_module().LocalLLMProvider(make_settings(), client=client)

    provider.close()
    provider.close()

    assert client.close_calls == 1


def test_cached_get_reuses_same_provider_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = provider_module()
    builds: list[object] = []

    def fake_build(settings: object, *, client: object | None = None) -> object:
        builds.append(settings)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(module, "get_settings", lambda: make_settings())
    monkeypatch.setattr(module, "build_llm_provider", fake_build)

    first = module.get_llm_provider()
    second = module.get_llm_provider()

    assert first is second
    assert len(builds) == 1


def test_concurrent_cached_get_constructs_one_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = provider_module()
    build_count = 0
    build_count_lock = Lock()

    def fake_build(settings: object, *, client: object | None = None) -> object:
        nonlocal build_count
        with build_count_lock:
            build_count += 1
        time.sleep(0.02)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(module, "get_settings", lambda: make_settings())
    monkeypatch.setattr(module, "build_llm_provider", fake_build)

    with ThreadPoolExecutor(max_workers=8) as executor:
        providers = list(executor.map(lambda _: module.get_llm_provider(), range(8)))

    assert len({id(provider) for provider in providers}) == 1
    assert build_count == 1


def test_cache_clear_is_idempotent_and_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = provider_module()
    providers: list[SimpleNamespace] = []

    def fake_build(settings: object, *, client: object | None = None) -> object:
        provider = SimpleNamespace(close_calls=0)

        def close() -> None:
            provider.close_calls += 1

        provider.close = close
        providers.append(provider)
        return provider

    monkeypatch.setattr(module, "get_settings", lambda: make_settings())
    monkeypatch.setattr(module, "build_llm_provider", fake_build)

    module.clear_llm_provider_cache()
    first = module.get_llm_provider()
    module.clear_llm_provider_cache()
    module.clear_llm_provider_cache()
    second = module.get_llm_provider()

    assert first is not second
    assert providers[0].close_calls == 1
    assert providers[1].close_calls == 0
