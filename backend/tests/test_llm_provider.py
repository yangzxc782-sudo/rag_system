from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib
import inspect
import json
import traceback
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
    LLM_EMPTY_CONTENT,
    LLM_PARAMETER_UNSUPPORTED,
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
        self.role = "assistant"
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


def test_get_llm_provider_formal_signature_is_no_arg() -> None:
    module = provider_module()

    assert inspect.signature(module.get_llm_provider).parameters == {}
    with pytest.raises(TypeError):
        module.get_llm_provider(make_settings())
    with pytest.raises(TypeError):
        module.get_llm_provider(client=FakeOpenAIClient())


def test_api_provider_factory_builds_api_without_local_fallback(
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

    built = module.build_llm_provider(settings, client=FakeOpenAIClient())

    assert built.__class__.__name__ == "APILLMProvider"
    assert built.provider_name == "api"
    assert built.model == remote_model
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


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
    ],
)
def test_local_default_http_client_ignores_environment_proxies(
    base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.example.invalid:8899")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    provider = local_module().LocalLLMProvider(
        make_settings(llm_base_url=base_url)
    )
    transport = provider._transport

    assert transport._client is None
    sdk_client = transport._get_client()
    http_client = sdk_client._client

    try:
        assert isinstance(http_client, httpx.Client)
        assert http_client._trust_env is False
    finally:
        provider.close()


def test_cached_local_http_client_is_reused_and_closed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = provider_module()
    monkeypatch.setattr(module, "get_settings", make_settings)

    provider = module.get_llm_provider()
    transport = provider._transport
    assert transport._client is None

    first_sdk_client = transport._get_client()
    second_sdk_client = transport._get_client()
    http_client = first_sdk_client._client
    close_calls = 0
    original_close = first_sdk_client.close

    def tracked_close() -> None:
        nonlocal close_calls
        close_calls += 1
        original_close()

    first_sdk_client.close = tracked_close

    assert second_sdk_client is first_sdk_client
    assert http_client._trust_env is False
    assert http_client.is_closed is False

    module.clear_llm_provider_cache()
    module.clear_llm_provider_cache()

    assert close_calls == 1
    assert http_client.is_closed is True
    assert module._provider_cache is None


def test_local_http_client_is_closed_if_sdk_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.llm.openai_chat_transport as transport_module

    http_client = httpx.Client(trust_env=False)

    def make_http_client(**kwargs: object) -> httpx.Client:
        assert kwargs == {"trust_env": False}
        return http_client

    def fail_sdk_construction(**_kwargs: object) -> object:
        raise RuntimeError("sdk construction failed")

    fake_openai_module = SimpleNamespace(
        DefaultHttpxClient=make_http_client,
        OpenAI=fail_sdk_construction,
    )
    monkeypatch.setattr(
        transport_module,
        "import_module",
        lambda _name: fake_openai_module,
    )

    with pytest.raises(RuntimeError, match="sdk construction failed"):
        transport_module._default_openai_client_factory(
            base_url="http://127.0.0.1:11434/v1",
            api_key="ollama",
            timeout=120,
            max_retries=0,
            http_client_trust_env=False,
        )

    assert http_client.is_closed is True


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
    fake_client = FakeOpenAIClient(
        FakeCompletions(response=FakeCompletion(content='{"items":[]}'))
    )
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


def test_missing_openai_dependency_error_detaches_import_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.llm.openai_chat_transport as transport_module

    marker = "sensitive-import-failure"

    def fail_import(_name: str) -> object:
        raise RuntimeError(marker)

    monkeypatch.setattr(transport_module, "import_module", fail_import)
    provider = local_module().LocalLLMProvider(make_settings())
    sensitive_prompt = "sensitive prompt"

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt(sensitive_prompt))

    error = exc_info.value
    assert error.code == LLM_CONFIG_INVALID
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    assert marker not in rendered
    assert sensitive_prompt not in rendered


@pytest.mark.parametrize(
    ("completion", "expected_code"),
    [
        (FakeCompletion(choices=[]), LLM_RESPONSE_INVALID),
        (FakeCompletion(content=None), LLM_EMPTY_CONTENT),
        (
            FakeCompletion(content="", tool_calls=[object()]),
            LLM_RESPONSE_INVALID,
        ),
    ],
)
def test_invalid_local_response_maps_to_specific_error(
    completion: object,
    expected_code: str,
) -> None:
    provider = local_module().LocalLLMProvider(
        make_settings(),
        client=FakeOpenAIClient(FakeCompletions(response=completion)),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("问题"))

    assert exc_info.value.code == expected_code


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
