from __future__ import annotations

import builtins
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from app.core.errors import (
    LLM_CONFIG_INVALID,
    LLM_GENERATION_FAILED,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    BusinessError,
)
from app.llm import LLMGenerateRequest, get_llm_provider
from app.llm.openai_compatible import OpenAICompatibleLLMProvider


class FakeMessage:
    def __init__(self, content: str | None, reasoning: str | None = None) -> None:
        self.content = content
        self.reasoning = reasoning


class FakeChoice:
    def __init__(self, content: str | None, reasoning: str | None = None) -> None:
        self.message = FakeMessage(content, reasoning=reasoning)


class FakeCompletion:
    def __init__(self, content: str | None = "测试回答", reasoning: str | None = None) -> None:
        self.choices = [FakeChoice(content, reasoning=reasoning)]


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


def make_mock_transport_openai_client(
    captured_bodies: list[dict[str, object]],
) -> OpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"x-request-id": "req-m0-test"},
            json={
                "id": "chatcmpl-m0-test",
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
            },
        )

    return OpenAI(
        api_key="test-key-not-real",
        base_url="https://example.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


class APIConnectionError(Exception):
    pass


class APITimeoutError(Exception):
    pass


class APIStatusError(Exception):
    pass


def make_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "llm_provider": "openai_compatible",
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "qwen3:8b",
        "llm_api_key": "ollama",
        "llm_temperature": 0.2,
        "llm_max_tokens": 2048,
        "llm_timeout_seconds": 120,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_llm_package_import_does_not_create_real_client() -> None:
    import app.llm as llm

    assert llm.get_llm_provider is get_llm_provider


def test_get_llm_provider_returns_openai_compatible_provider() -> None:
    provider = get_llm_provider(make_settings())

    assert isinstance(provider, OpenAICompatibleLLMProvider)
    assert provider.provider_name == "openai_compatible"


def test_get_llm_provider_rejects_unknown_provider() -> None:
    with pytest.raises(BusinessError) as exc_info:
        get_llm_provider(make_settings(llm_provider="ollama"))

    assert exc_info.value.code == LLM_CONFIG_INVALID


def test_missing_config_raises_llm_config_invalid() -> None:
    with pytest.raises(BusinessError) as exc_info:
        OpenAICompatibleLLMProvider(make_settings(llm_model=""))

    assert exc_info.value.code == LLM_CONFIG_INVALID


def test_provider_does_not_create_client_before_generate() -> None:
    created: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeOpenAIClient:
        created.append(kwargs)
        return FakeOpenAIClient()

    OpenAICompatibleLLMProvider(make_settings(), client_factory=factory)

    assert created == []


def test_generate_passes_openai_compatible_parameters_and_returns_result() -> None:
    factory_calls: list[dict[str, object]] = []
    fake_client = FakeOpenAIClient()

    def factory(**kwargs: object) -> FakeOpenAIClient:
        factory_calls.append(kwargs)
        return fake_client

    provider = OpenAICompatibleLLMProvider(make_settings(), client_factory=factory)
    result = provider.generate(
        LLMGenerateRequest.from_prompt(
            "用户问题",
            "系统提示",
            temperature=0.3,
            max_tokens=512,
        )
    )

    assert result.text == "测试回答"
    assert result.provider == "openai_compatible"
    assert result.model == "qwen3:8b"
    assert factory_calls == [
        {
            "base_url": "http://localhost:11434/v1",
            "api_key": "ollama",
            "timeout": 120,
        }
    ]
    assert fake_client.completions.calls == [
        {
            "model": "qwen3:8b",
            "messages": [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "用户问题"},
            ],
            "temperature": 0.3,
            "max_tokens": 512,
        }
    ]
    assert "response_format" not in fake_client.completions.calls[0]
    assert "extra_body" not in fake_client.completions.calls[0]


def test_generate_passes_optional_json_mode_and_think_parameters() -> None:
    fake_client = FakeOpenAIClient()
    provider = OpenAICompatibleLLMProvider(make_settings(), client=fake_client)

    provider.generate(
        LLMGenerateRequest.from_prompt(
            '{"items":[]}',
            json_mode=True,
            think=False,
        )
    )

    assert fake_client.completions.calls[0]["response_format"] == {"type": "json_object"}
    assert fake_client.completions.calls[0]["extra_body"] == {"think": False}


def test_openai_sdk_merges_json_mode_and_think_into_wire_body() -> None:
    captured_bodies: list[dict[str, object]] = []
    client = make_mock_transport_openai_client(captured_bodies)
    provider = OpenAICompatibleLLMProvider(make_settings(), client=client)

    try:
        provider.generate(
            LLMGenerateRequest.from_prompt(
                '{"items":[]}',
                json_mode=True,
                think=False,
            )
        )
    finally:
        client.close()

    assert captured_bodies[0]["think"] is False
    assert captured_bodies[0]["response_format"] == {"type": "json_object"}


def test_openai_sdk_does_not_send_think_when_request_omits_it() -> None:
    captured_bodies: list[dict[str, object]] = []
    client = make_mock_transport_openai_client(captured_bodies)
    provider = OpenAICompatibleLLMProvider(make_settings(), client=client)

    try:
        provider.generate(LLMGenerateRequest.from_prompt("plain request"))
    finally:
        client.close()

    assert "think" not in captured_bodies[0]


def test_generate_without_system_prompt_sends_user_message_only() -> None:
    fake_client = FakeOpenAIClient()
    provider = OpenAICompatibleLLMProvider(make_settings(), client=fake_client)

    provider.generate(LLMGenerateRequest.from_prompt("只包含用户问题"))

    assert fake_client.completions.calls[0]["messages"] == [
        {"role": "user", "content": "只包含用户问题"}
    ]


def test_connection_failure_maps_to_llm_unavailable() -> None:
    provider = OpenAICompatibleLLMProvider(
        make_settings(),
        client=FakeOpenAIClient(FakeCompletions(exc=APIConnectionError("down"))),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("问题"))

    assert exc_info.value.code == LLM_UNAVAILABLE


def test_timeout_maps_to_llm_timeout() -> None:
    provider = OpenAICompatibleLLMProvider(
        make_settings(),
        client=FakeOpenAIClient(FakeCompletions(exc=APITimeoutError("timeout"))),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("问题"))

    assert exc_info.value.code == LLM_TIMEOUT


def test_status_error_maps_to_llm_generation_failed() -> None:
    provider = OpenAICompatibleLLMProvider(
        make_settings(),
        client=FakeOpenAIClient(FakeCompletions(exc=APIStatusError("bad response"))),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("问题"))

    assert exc_info.value.code == LLM_GENERATION_FAILED


def test_response_without_text_maps_to_llm_generation_failed() -> None:
    provider = OpenAICompatibleLLMProvider(
        make_settings(),
        client=FakeOpenAIClient(FakeCompletions(response=FakeCompletion(content=None))),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("问题"))

    assert exc_info.value.code == LLM_GENERATION_FAILED


def test_response_with_reasoning_but_without_content_still_maps_to_llm_generation_failed() -> None:
    provider = OpenAICompatibleLLMProvider(
        make_settings(),
        client=FakeOpenAIClient(
            FakeCompletions(response=FakeCompletion(content=None, reasoning='{"items":[]}'))
        ),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("问题"))

    assert exc_info.value.code == LLM_GENERATION_FAILED


def test_generate_does_not_print_api_key_or_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    printed: list[tuple[object, ...]] = []

    def fake_print(*args: object, **_kwargs: object) -> None:
        printed.append(args)

    monkeypatch.setattr(builtins, "print", fake_print)
    provider = OpenAICompatibleLLMProvider(make_settings(), client=FakeOpenAIClient())

    provider.generate(
        LLMGenerateRequest.from_prompt("不要打印的问题", "不要打印的系统提示")
    )

    assert printed == []
