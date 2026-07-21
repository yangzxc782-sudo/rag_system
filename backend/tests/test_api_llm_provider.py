from __future__ import annotations

import json
import traceback
from types import SimpleNamespace
from typing import Any

import httpx
import openai
from openai import OpenAI
from pydantic import SecretStr
import pytest

from app.core.errors import (
    LLM_AUTHENTICATION_FAILED,
    LLM_EMPTY_CONTENT,
    LLM_JSON_INVALID,
    LLM_MODEL_NOT_FOUND,
    LLM_PARAMETER_UNSUPPORTED,
    LLM_PERMISSION_DENIED,
    LLM_RATE_LIMITED,
    LLM_REQUEST_REJECTED,
    LLM_RESPONSE_INVALID,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    LLM_UPSTREAM_FAILED,
    BusinessError,
)
from app.llm import (
    LLMFunctionTool,
    LLMGenerateRequest,
    LLMImageURLContentPart,
    LLMMessage,
    LLMTextContentPart,
    clear_llm_provider_cache,
)
from app.llm.api import APILLMProvider


class FakeMessage:
    def __init__(
        self,
        content: object,
        *,
        role: str = "assistant",
        tool_calls: list[object] | None = None,
    ) -> None:
        self.role = role
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(
        self,
        content: object,
        *,
        role: str = "assistant",
        tool_calls: list[object] | None = None,
    ) -> None:
        self.message = FakeMessage(
            content,
            role=role,
            tool_calls=tool_calls,
        )


class FakeUsage:
    prompt_tokens = 21
    completion_tokens = 8
    total_tokens = 29


class FakeCompletion:
    def __init__(
        self,
        content: object = "remote answer",
        *,
        choices: list[object] | None = None,
        role: str = "assistant",
        tool_calls: list[object] | None = None,
    ) -> None:
        self.id = "chatcmpl-remote-test"
        self.choices = choices if choices is not None else [
            FakeChoice(content, role=role, tool_calls=tool_calls)
        ]
        self.usage = FakeUsage()


class FakeCompletions:
    def __init__(
        self,
        *,
        response: object | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.response = FakeCompletion() if response is None else response
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


def api_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "llm_provider": "api",
        "llm_temperature": 0.2,
        "llm_max_tokens": 2048,
        "llm_remote_base_url": "https://remote.example.invalid/v1",
        "llm_remote_api_key": "test-remote-key-not-real",
        "llm_remote_model": "remote-model",
        "llm_remote_timeout_seconds": 60,
        "llm_remote_supports_json_mode": False,
        "llm_remote_allow_insecure_http": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def text_message(role: str, text: str) -> LLMMessage:
    return LLMMessage(
        role=role,  # type: ignore[arg-type]
        content=(LLMTextContentPart(text=text),),
    )


def make_wire_client(captured_bodies: list[dict[str, object]]) -> OpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "req-remote-test"},
            json={
                "id": "chatcmpl-remote-wire",
                "object": "chat.completion",
                "created": 1,
                "model": "remote-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"answer":"ok"}',
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 14,
                    "completion_tokens": 6,
                    "total_tokens": 20,
                },
            },
        )

    return OpenAI(
        api_key="test-wire-key-not-real",
        base_url="https://wire.example.invalid/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def make_status_error(
    error_class: type[openai.APIStatusError],
    status_code: int,
) -> openai.APIStatusError:
    request = httpx.Request(
        "POST",
        "https://remote.example.invalid/v1/chat/completions",
        headers={"Authorization": "Bearer test-remote-key-not-real"},
    )
    response = httpx.Response(
        status_code,
        request=request,
        headers={
            "Retry-After": "60",
            "X-Remote-Secret": "remote-header-secret",
        },
        json={"error": {"message": "remote body secret"}},
    )
    return error_class(
        "remote body secret",
        response=response,
        body={"error": {"message": "remote body secret"}},
    )


@pytest.fixture(autouse=True)
def clear_provider_cache_between_tests():
    clear_llm_provider_cache()
    yield
    clear_llm_provider_cache()


def test_api_json_capability_defaults_false_and_accepts_explicit_true() -> None:
    default_provider = APILLMProvider(
        api_settings(),
        client=FakeOpenAIClient(),
    )
    enabled_provider = APILLMProvider(
        api_settings(llm_remote_supports_json_mode=True),
        client=FakeOpenAIClient(),
    )

    assert default_provider.capabilities.supports_json_mode is False
    assert enabled_provider.capabilities.supports_json_mode is True
    assert enabled_provider.capabilities.supports_think is False
    assert enabled_provider.capabilities.supports_tools is False
    assert enabled_provider.capabilities.supports_parallel_tool_calls is False
    assert enabled_provider.capabilities.supports_image_input is False


def test_api_rejects_disabled_json_mode_before_client_call() -> None:
    client = FakeOpenAIClient()
    provider = APILLMProvider(api_settings(), client=client)

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(
            LLMGenerateRequest.from_prompt("return json", json_mode=True)
        )

    assert exc_info.value.code == LLM_PARAMETER_UNSUPPORTED
    assert exc_info.value.detail == {
        "provider": "api",
        "parameter": "json_mode",
        "required_capability": "supports_json_mode",
    }
    assert client.completions.calls == []


def test_api_json_mode_and_four_turn_history_reach_wire_without_think() -> None:
    captured_bodies: list[dict[str, object]] = []
    client = make_wire_client(captured_bodies)
    provider = APILLMProvider(
        api_settings(llm_remote_supports_json_mode=True),
        client=client,
    )
    request = LLMGenerateRequest(
        messages=(
            text_message("system", "system rules"),
            text_message("user", "turn 1"),
            text_message("assistant", "answer 1"),
            text_message("user", "turn 2"),
        ),
        json_mode=True,
        think=False,
    )

    try:
        result = provider.generate(request)
    finally:
        provider.close()

    assert result.text == '{"answer":"ok"}'
    assert captured_bodies == [
        {
            "model": "remote-model",
            "messages": [
                {"role": "system", "content": "system rules"},
                {"role": "user", "content": "turn 1"},
                {"role": "assistant", "content": "answer 1"},
                {"role": "user", "content": "turn 2"},
            ],
            "temperature": 0.2,
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
        }
    ]
    assert "think" not in captured_bodies[0]


def test_api_advisory_think_false_is_omitted() -> None:
    client = FakeOpenAIClient()
    provider = APILLMProvider(api_settings(), client=client)

    provider.generate(LLMGenerateRequest.from_prompt("question", think=False))

    assert "extra_body" not in client.completions.calls[0]
    assert "think" not in client.completions.calls[0]


def test_api_advisory_think_true_and_plain_mode_are_omitted_from_wire() -> None:
    captured_bodies: list[dict[str, object]] = []
    client = make_wire_client(captured_bodies)
    provider = APILLMProvider(api_settings(), client=client)

    try:
        provider.generate(
            LLMGenerateRequest.from_prompt(
                "question",
                json_mode=False,
                think=True,
                think_required=False,
            )
        )
    finally:
        provider.close()

    assert len(captured_bodies) == 1
    assert "think" not in captured_bodies[0]
    assert "extra_body" not in captured_bodies[0]
    assert "response_format" not in captured_bodies[0]


def test_api_required_think_is_rejected_before_client_call() -> None:
    client = FakeOpenAIClient()
    provider = APILLMProvider(api_settings(), client=client)

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(
            LLMGenerateRequest.from_prompt(
                "question",
                think=False,
                think_required=True,
            )
        )

    assert exc_info.value.code == LLM_PARAMETER_UNSUPPORTED
    assert exc_info.value.detail == {
        "provider": "api",
        "parameter": "think",
        "required_capability": "supports_think",
    }
    assert client.completions.calls == []


@pytest.mark.parametrize(
    ("llm_request", "parameter"),
    [
        (
            LLMGenerateRequest(
                messages=(text_message("user", "use tool"),),
                tools=(
                    LLMFunctionTool(
                        name="lookup",
                        parameters={"type": "object", "properties": {}},
                    ),
                ),
            ),
            "tools",
        ),
        (
            LLMGenerateRequest(
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
            ),
            "image_input",
        ),
        (
            LLMGenerateRequest(
                messages=(text_message("user", "parallel"),),
                parallel_tool_calls=True,
            ),
            "parallel_tool_calls",
        ),
    ],
)
def test_api_unsupported_capabilities_are_rejected_before_client_call(
    llm_request: LLMGenerateRequest,
    parameter: str,
) -> None:
    client = FakeOpenAIClient()
    provider = APILLMProvider(api_settings(), client=client)

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(llm_request)

    assert exc_info.value.code == LLM_PARAMETER_UNSUPPORTED
    assert exc_info.value.detail["provider"] == "api"
    assert exc_info.value.detail["parameter"] == parameter
    assert client.completions.calls == []


def test_api_constructs_lazy_client_with_remote_fields_and_zero_retries() -> None:
    created: list[dict[str, object]] = []
    client = FakeOpenAIClient()

    def factory(**kwargs: object) -> FakeOpenAIClient:
        created.append(kwargs)
        return client

    provider = APILLMProvider(api_settings(), client_factory=factory)

    assert created == []
    provider.generate(LLMGenerateRequest.from_prompt("question"))

    assert created == [
        {
            "base_url": "https://remote.example.invalid/v1",
            "api_key": "test-remote-key-not-real",
            "timeout": 60,
            "max_retries": 0,
        }
    ]


def test_api_keeps_secret_wrapped_until_lazy_client_creation() -> None:
    class CountingSecretStr(SecretStr):
        calls = 0

        def get_secret_value(self) -> str:
            type(self).calls += 1
            return super().get_secret_value()

    secret_value = "test-lazy-secret-not-real"
    secret = CountingSecretStr(secret_value)
    created: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeOpenAIClient:
        created.append(kwargs)
        return FakeOpenAIClient()

    provider = APILLMProvider(
        api_settings(llm_remote_api_key=secret),
        client_factory=factory,
    )
    transport = provider._transport

    # One temporary read is allowed for active configuration validation.
    assert secret.calls == 1
    assert created == []
    assert not hasattr(transport, "api_key")
    assert isinstance(transport._api_key, SecretStr)
    assert all(value != secret_value for value in vars(transport).values())
    assert secret_value not in repr(transport)
    assert secret_value not in repr(provider)

    provider.generate(LLMGenerateRequest.from_prompt("question"))

    assert secret.calls == 2
    assert created == [
        {
            "base_url": "https://remote.example.invalid/v1",
            "api_key": secret_value,
            "timeout": 60,
            "max_retries": 0,
        }
    ]


def test_api_does_not_read_local_fields_or_fallback_to_local() -> None:
    class APISentinelSettings:
        llm_provider = "api"
        llm_temperature = 0.2
        llm_max_tokens = 2048
        llm_remote_base_url = "https://remote.example.invalid/v1"
        llm_remote_api_key = "test-remote-key-not-real"
        llm_remote_model = "remote-model"
        llm_remote_timeout_seconds = 60
        llm_remote_supports_json_mode = False
        llm_remote_allow_insecure_http = False

        def __getattribute__(self, name: str) -> object:
            if name in {"llm_base_url", "llm_model", "llm_api_key"}:
                raise AssertionError(f"API path read local setting: {name}")
            return object.__getattribute__(self, name)

    provider = APILLMProvider(
        APISentinelSettings(),
        client=FakeOpenAIClient(),
    )

    assert provider.provider_name == "api"
    assert provider.model == "remote-model"


def test_api_success_returns_assistant_message_usage_and_request_id() -> None:
    provider = APILLMProvider(api_settings(), client=FakeOpenAIClient())

    result = provider.generate(LLMGenerateRequest.from_prompt("question"))

    assert result.message == text_message("assistant", "remote answer")
    assert result.provider == "api"
    assert result.model == "remote-model"
    assert result.usage is not None
    assert result.usage.prompt_tokens == 21
    assert result.usage.completion_tokens == 8
    assert result.usage.total_tokens == 29
    assert result.request_id == "chatcmpl-remote-test"


@pytest.mark.parametrize(
    ("exc", "expected_code", "expected_status"),
    [
        (make_status_error(openai.AuthenticationError, 401), LLM_AUTHENTICATION_FAILED, 502),
        (make_status_error(openai.PermissionDeniedError, 403), LLM_PERMISSION_DENIED, 502),
        (make_status_error(openai.NotFoundError, 404), LLM_MODEL_NOT_FOUND, 502),
        (openai.APITimeoutError(httpx.Request("POST", "https://example.invalid")), LLM_TIMEOUT, 504),
        (openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid")), LLM_UNAVAILABLE, 503),
        (make_status_error(openai.RateLimitError, 429), LLM_RATE_LIMITED, 429),
        (make_status_error(openai.InternalServerError, 500), LLM_UPSTREAM_FAILED, 502),
        (make_status_error(openai.BadRequestError, 400), LLM_REQUEST_REJECTED, 502),
    ],
)
def test_api_maps_upstream_errors_without_retry(
    exc: Exception,
    expected_code: str,
    expected_status: int,
) -> None:
    client = FakeOpenAIClient(FakeCompletions(exc=exc))
    provider = APILLMProvider(api_settings(), client=client)

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("question"))

    assert exc_info.value.code == expected_code
    assert exc_info.value.status_code == expected_status
    assert len(client.completions.calls) == 1


def test_api_rate_limit_detail_is_retryable_and_excludes_remote_data() -> None:
    client = FakeOpenAIClient(
        FakeCompletions(
            exc=make_status_error(openai.RateLimitError, 429),
        )
    )
    provider = APILLMProvider(api_settings(), client=client)

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("question"))

    error = exc_info.value
    assert error.code == LLM_RATE_LIMITED
    assert error.status_code == 429
    assert error.detail == {
        "source": "upstream_llm",
        "error_type": "RateLimitError",
        "upstream_status": 429,
        "retryable": True,
    }
    rendered = f"{error} {error!r} {error.detail!r}"
    for forbidden in (
        "Retry-After",
        "remote-header-secret",
        "remote body secret",
        "Authorization",
        "remote.example.invalid",
        "remote-model",
        "test-remote-key-not-real",
    ):
        assert forbidden not in rendered
    assert len(client.completions.calls) == 1


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        ("not-json", LLM_RESPONSE_INVALID),
        (FakeCompletion(choices=[]), LLM_RESPONSE_INVALID),
        (FakeCompletion(content=None), LLM_EMPTY_CONTENT),
        (FakeCompletion(content=""), LLM_EMPTY_CONTENT),
        (FakeCompletion(content="   "), LLM_EMPTY_CONTENT),
        (FakeCompletion(content="answer", role="user"), LLM_RESPONSE_INVALID),
        (
            FakeCompletion(content="answer", tool_calls=[SimpleNamespace()]),
            LLM_RESPONSE_INVALID,
        ),
    ],
)
def test_api_rejects_invalid_or_unsupported_responses(
    response: object,
    expected_code: str,
) -> None:
    provider = APILLMProvider(
        api_settings(),
        client=FakeOpenAIClient(FakeCompletions(response=response)),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("question"))

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("content", "received_type"),
    [
        ([{"type": "text", "text": "unexpected"}], "list"),
        ({"type": "text", "text": "unexpected"}, "dict"),
    ],
)
def test_api_structured_content_is_response_invalid_without_content_echo(
    content: object,
    received_type: str,
) -> None:
    provider = APILLMProvider(
        api_settings(),
        client=FakeOpenAIClient(
            FakeCompletions(response=FakeCompletion(content=content))
        ),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(LLMGenerateRequest.from_prompt("question"))

    assert exc_info.value.code == LLM_RESPONSE_INVALID
    assert exc_info.value.detail == {
        "field": "choices[0].message.content",
        "received_type": received_type,
    }
    assert "unexpected" not in str(exc_info.value.detail)


@pytest.mark.parametrize("content", ["not-json", "[]", '"scalar"'])
def test_api_json_mode_requires_valid_json_object(content: str) -> None:
    provider = APILLMProvider(
        api_settings(llm_remote_supports_json_mode=True),
        client=FakeOpenAIClient(
            FakeCompletions(response=FakeCompletion(content=content))
        ),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(
            LLMGenerateRequest.from_prompt("return json", json_mode=True)
        )

    assert exc_info.value.code == LLM_JSON_INVALID
    assert exc_info.value.status_code == 502


def test_api_errors_and_tracebacks_do_not_expose_remote_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    remote_url = "https://remote.example.invalid/v1"
    remote_key = "test-remote-key-not-real"
    remote_model = "remote-model"
    remote_body = "remote body secret"
    sensitive_prompt = "sensitive prompt contents"
    provider = APILLMProvider(
        api_settings(
            llm_remote_base_url=remote_url,
            llm_remote_api_key=remote_key,
            llm_remote_model=remote_model,
        ),
        client=FakeOpenAIClient(
            FakeCompletions(
                exc=make_status_error(openai.BadRequestError, 400)
            )
        ),
    )

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(
            LLMGenerateRequest.from_prompt(sensitive_prompt)
        )

    rendered = "".join(traceback.format_exception(exc_info.value))
    for secret in (
        remote_url,
        remote_key,
        remote_model,
        remote_body,
        "Authorization",
        sensitive_prompt,
    ):
        assert secret not in rendered
        assert secret not in caplog.text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert all(
        not isinstance(value, (openai.APIError, httpx.Request, httpx.Response))
        for value in vars(exc_info.value).values()
    )
    safe_renderings = (
        str(exc_info.value),
        repr(exc_info.value),
        rendered,
    )
    for value in safe_renderings:
        assert remote_key not in value
        assert remote_url not in value
        assert remote_model not in value
        assert remote_body not in value


def test_cached_api_provider_reuses_instance_and_clear_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.llm.openai_chat_transport as transport_module
    import app.llm.provider as provider_module

    client = FakeOpenAIClient()
    created: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeOpenAIClient:
        created.append(kwargs)
        return client

    monkeypatch.setattr(provider_module, "get_settings", api_settings)
    monkeypatch.setattr(
        transport_module,
        "_default_openai_client_factory",
        factory,
    )

    first = provider_module.get_llm_provider()
    second = provider_module.get_llm_provider()

    assert isinstance(first, APILLMProvider)
    assert second is first
    first.generate(LLMGenerateRequest.from_prompt("question"))
    assert len(created) == 1

    provider_module.clear_llm_provider_cache()

    assert client.close_calls == 1
    assert provider_module._provider_cache is None
