from __future__ import annotations

import json
import logging
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
from app.llm.local import LocalLLMProvider


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


def test_api_capability_rejection_logs_only_safe_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeOpenAIClient()
    provider = APILLMProvider(api_settings(), client=client)
    sensitive_prompt = "sensitive capability prompt"
    caplog.set_level(logging.WARNING, logger="app.llm.provider")

    with pytest.raises(BusinessError):
        provider.generate(
            LLMGenerateRequest.from_prompt(sensitive_prompt, json_mode=True)
        )

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "llm_capability_rejected"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.provider == "api"
    assert record.operation == "capability_preflight"
    assert record.error_code == LLM_PARAMETER_UNSUPPORTED
    assert record.capability == "supports_json_mode"
    assert record.retryable is False
    assert client.completions.calls == []
    assert sensitive_prompt not in caplog.text


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
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeOpenAIClient()
    provider = APILLMProvider(api_settings(), client=client)
    caplog.set_level(logging.WARNING, logger="app.llm.provider")

    with pytest.raises(BusinessError) as exc_info:
        provider.generate(llm_request)

    assert exc_info.value.code == LLM_PARAMETER_UNSUPPORTED
    assert exc_info.value.detail["provider"] == "api"
    assert exc_info.value.detail["parameter"] == parameter
    assert client.completions.calls == []
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "llm_capability_rejected"
    ]
    assert len(records) == 1
    assert records[0].capability == exc_info.value.detail["required_capability"]
    rendered = f"{records[0].getMessage()} {records[0].__dict__!r}"
    assert "lookup" not in rendered
    assert "secret.png" not in rendered


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


def test_api_default_http_client_keeps_environment_proxy_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.example.invalid:8899")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    provider = APILLMProvider(api_settings())
    transport = provider._transport

    assert transport._client is None
    sdk_client = transport._get_client()
    http_client = sdk_client._client

    try:
        assert isinstance(http_client, httpx.Client)
        assert http_client._trust_env is True
    finally:
        provider.close()


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


def test_api_success_logs_safe_result_metadata_without_request_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_prompt = "sensitive successful prompt"
    caplog.set_level(logging.INFO, logger="app.llm.openai_chat_transport")
    provider = APILLMProvider(api_settings(), client=FakeOpenAIClient())

    provider.generate(LLMGenerateRequest.from_prompt(sensitive_prompt))

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "llm_generation_completed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.provider == "api"
    assert record.model == "remote-model"
    assert record.operation == "chat.completions"
    assert isinstance(record.latency_ms, int)
    assert record.latency_ms >= 0
    assert record.request_id == "chatcmpl-remote-test"
    assert record.prompt_tokens == 21
    assert record.completion_tokens == 8
    assert record.total_tokens == 29
    assert sensitive_prompt not in caplog.text


def test_untrusted_request_id_is_preserved_in_result_but_omitted_from_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    untrusted_request_id = "remote body secret as request id"
    completion = FakeCompletion()
    completion.id = untrusted_request_id
    caplog.set_level(logging.INFO, logger="app.llm.openai_chat_transport")
    provider = APILLMProvider(
        api_settings(),
        client=FakeOpenAIClient(FakeCompletions(response=completion)),
    )

    result = provider.generate(LLMGenerateRequest.from_prompt("question"))

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "llm_generation_completed"
    )
    assert result.request_id == untrusted_request_id
    assert record.request_id is None
    assert untrusted_request_id not in caplog.text
    assert untrusted_request_id not in repr(record.__dict__)


def test_local_and_api_response_errors_use_provider_neutral_wording() -> None:
    local_settings = SimpleNamespace(
        llm_provider="local",
        llm_temperature=0.2,
        llm_max_tokens=2048,
        llm_base_url="http://localhost:11434/v1",
        llm_model="local-model",
        llm_api_key="",
        llm_timeout_seconds=120,
    )
    providers = (
        LocalLLMProvider(
            local_settings,
            client=FakeOpenAIClient(
                FakeCompletions(response=FakeCompletion(content=None))
            ),
        ),
        APILLMProvider(
            api_settings(),
            client=FakeOpenAIClient(
                FakeCompletions(response=FakeCompletion(content=None))
            ),
        ),
    )

    messages: list[str] = []
    for provider in providers:
        with pytest.raises(BusinessError) as exc_info:
            provider.generate(LLMGenerateRequest.from_prompt("question"))
        messages.append(exc_info.value.message)

    assert messages == [
        "LLM response did not contain generated text.",
        "LLM response did not contain generated text.",
    ]
    assert all("local" not in message.lower() for message in messages)
    assert all("api" not in message.lower() for message in messages)
    assert all("ollama" not in message.lower() for message in messages)


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
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert set(exc_info.value.detail) <= {
        "source",
        "error_type",
        "upstream_status",
        "retryable",
    }
    assert all(
        not isinstance(value, (openai.APIError, httpx.Request, httpx.Response))
        for value in vars(exc_info.value).values()
    )


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


def test_api_failure_log_is_structured_and_excludes_upstream_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_prompt = "sensitive failed prompt"
    caplog.set_level(logging.WARNING, logger="app.llm.openai_chat_transport")
    client = FakeOpenAIClient(
        FakeCompletions(exc=make_status_error(openai.RateLimitError, 429))
    )
    provider = APILLMProvider(api_settings(), client=client)

    with pytest.raises(BusinessError):
        provider.generate(LLMGenerateRequest.from_prompt(sensitive_prompt))

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "llm_generation_failed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.provider == "api"
    assert record.model == "remote-model"
    assert record.operation == "chat.completions"
    assert record.error_code == LLM_RATE_LIMITED
    assert record.upstream_status == 429
    assert record.retryable is True
    assert isinstance(record.latency_ms, int)
    rendered_record = f"{record.getMessage()} {record.__dict__!r}"
    for forbidden in (
        sensitive_prompt,
        "test-remote-key-not-real",
        "remote.example.invalid/v1",
        "Authorization",
        "Retry-After",
        "remote-header-secret",
        "remote body secret",
    ):
        assert forbidden not in rendered_record


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
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize("content,finish_reason,code,failure_kind,top_level_type", [
    ('{"entities":[],"relationships":[]}', "stop", None, None, None),
    (' \n{"entities":[],"relationships":[]}\t', "stop", None, None, None),
    ('{"entities":[],"relationships":[]}', "length", None, None, None),
    ('```json\n{"entities":[],"relationships":[]}\n```', "stop", LLM_JSON_INVALID, "decode_error", None),
    ('以下是结果：\n{"entities":[],"relationships":[]}', "stop", LLM_JSON_INVALID, "decode_error", None),
    ('[]', "stop", LLM_JSON_INVALID, "top_level_not_object", "list"),
    ('null', "stop", LLM_JSON_INVALID, "top_level_not_object", "NoneType"),
    ('"scalar"', "stop", LLM_JSON_INVALID, "top_level_not_object", "str"),
    ('1', "stop", LLM_JSON_INVALID, "top_level_not_object", "int"),
    ('1.5', "stop", LLM_JSON_INVALID, "top_level_not_object", "float"),
    ('true', "stop", LLM_JSON_INVALID, "top_level_not_object", "bool"),
    (None, "stop", LLM_EMPTY_CONTENT, None, None),
    ('  ', "stop", LLM_EMPTY_CONTENT, None, None),
    ([], "stop", LLM_RESPONSE_INVALID, None, None),
    ({"entities": []}, "stop", LLM_RESPONSE_INVALID, None, None),
    ('{"entities":[{"name":"abc"', "length", LLM_JSON_INVALID, "decode_error", None),
    ('{\n  "entities": ]\n}', "stop", LLM_JSON_INVALID, "decode_error", None),
])
def test_api_json_failure_diagnostics_preserve_acceptance(
    content, finish_reason, code, failure_kind, top_level_type, caplog,
):
    completion = FakeCompletion(content=content)
    completion.choices[0].finish_reason = finish_reason
    client = FakeOpenAIClient(FakeCompletions(response=completion))
    provider = APILLMProvider(api_settings(llm_remote_supports_json_mode=True, llm_max_tokens=1024), client=client)
    request = LLMGenerateRequest.from_prompt("synthetic prompt", json_mode=True)
    with caplog.at_level(logging.INFO):
        if code is None:
            result = provider.generate(request)
            assert result.text == content
            assert not hasattr(result, "finish_reason")  # Including valid JSON + length: unchanged result contract.
        else:
            with pytest.raises(BusinessError) as caught:
                provider.generate(request)
            assert caught.value.code == code and caught.value.status_code == 502
            assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert client.completions.calls[0]["max_tokens"] == 1024
    assert client.completions.calls[0]["response_format"] == {"type": "json_object"}
    failures = [r for r in caplog.records if getattr(r, "event", None) == "llm_generation_failed"]
    if code is None:
        assert not failures
        return
    record, = failures
    if code != LLM_JSON_INVALID:
        assert not hasattr(record, "json_failure_kind")
        return
    # Diagnostics remain log-only; public error detail/traceback shape is unchanged.
    assert caught.value.detail == {"field": "choices[0].message.content"}
    assert record.json_failure_kind == failure_kind
    assert record.finish_reason == finish_reason
    assert record.content_type == "str" and record.content_length == len(content)
    assert record.provider == "api" and record.model == "remote-model"
    assert record.json_mode is True and record.effective_max_tokens == 1024
    assert record.prompt_tokens == 21 and record.completion_tokens == 8 and record.total_tokens == 29
    assert record.starts_with_object == content.strip().startswith("{")
    assert record.ends_with_object == content.strip().endswith("}")
    assert record.contains_fence == ("```" in content)
    assert record.upstream_status is None and record.retryable is False
    if failure_kind == "decode_error":
        with pytest.raises(json.JSONDecodeError) as decoded:
            json.loads(content)
        assert (record.json_error_msg, record.json_error_pos, record.json_error_lineno, record.json_error_colno) == (
            decoded.value.msg, decoded.value.pos, decoded.value.lineno, decoded.value.colno)
    else:
        assert record.top_level_type == top_level_type
        assert not hasattr(record, "json_error_pos")
    # The default %(message)s console formatter can show the safe diagnostic object.
    console_diagnostics = json.loads(record.getMessage().split(" json_diagnostics=", 1)[1])
    for name, value in console_diagnostics.items():
        assert getattr(record, name) == value


@pytest.mark.parametrize("max_tokens,expected", [(None, 1024), (257, 257)])
def test_api_json_failure_diagnostics_use_effective_request_budget(max_tokens, expected, caplog):
    client = FakeOpenAIClient(FakeCompletions(response=FakeCompletion(content="[]")))
    provider = APILLMProvider(api_settings(llm_remote_supports_json_mode=True, llm_max_tokens=1024), client=client)
    with pytest.raises(BusinessError):
        provider.generate(LLMGenerateRequest.from_prompt("synthetic", json_mode=True, max_tokens=max_tokens))
    record, = [r for r in caplog.records if getattr(r, "event", None) == "llm_generation_failed"]
    assert record.effective_max_tokens == client.completions.calls[0]["max_tokens"] == expected


def test_api_json_failure_diagnostics_from_sdk_mock_transport(caplog):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chatcmpl-json-diagnostic", "object": "chat.completion", "created": 1, "model": "remote-model",
            "choices": [{"index": 0, "finish_reason": "length",
                         "message": {"role": "assistant", "content": '{"entities":['}}],
            "usage": {"prompt_tokens": 4000, "completion_tokens": 1024, "total_tokens": 5024},
        })
    client = OpenAI(api_key="test-wire-key-not-real", base_url="https://wire.example.invalid/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    provider = APILLMProvider(api_settings(llm_remote_supports_json_mode=True, llm_max_tokens=1024), client=client)
    try:
        with pytest.raises(BusinessError) as caught:
            provider.generate(LLMGenerateRequest.from_prompt("synthetic", json_mode=True, temperature=0.1))
    finally:
        provider.close()
    assert caught.value.code == LLM_JSON_INVALID
    assert captured == [{"model": "remote-model", "messages": [{"role": "user", "content": "synthetic"}],
        "max_tokens": 1024, "temperature": 0.1, "response_format": {"type": "json_object"}}]
    record, = [r for r in caplog.records if getattr(r, "event", None) == "llm_generation_failed"]
    assert record.finish_reason == "length" and record.json_failure_kind == "decode_error"
    assert record.effective_max_tokens == record.completion_tokens == 1024
    assert record.prompt_tokens == 4000 and record.total_tokens == 5024


@pytest.mark.parametrize("budget", [8192, 4096])
def test_m2_budget_reaches_sdk_and_diagnostics_without_changing_generic_default(budget, caplog):
    from app.core.config import Settings
    from app.extraction.kg_extract import extract_piece
    from test_kg_v2_protocol_units import anchor
    from app.llm.openai_chat_transport import _generation_diagnostics

    caplog.set_level(logging.INFO)
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        truncated = len(captured) == 3
        content = '{"entities":[' if truncated else '{"entities":[],"relationships":[]}'
        return httpx.Response(200, json={
            "id": "chatcmpl-kg-budget", "object": "chat.completion", "created": 1, "model": "remote-model",
            "choices": [{"index": 0, "finish_reason": "length" if truncated else "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 4000, "completion_tokens": budget if truncated else 8,
                      "total_tokens": 4000 + (budget if truncated else 8)},
        })

    settings = Settings(_env_file=None, llm_provider="api", llm_max_tokens=1024, kg_llm_max_tokens=budget,
        llm_remote_supports_json_mode=True, llm_remote_model="remote-model",
        llm_remote_base_url="https://wire.example.invalid/v1", llm_remote_api_key="test-wire-key-not-real")
    client = OpenAI(api_key="test-wire-key-not-real", base_url=settings.llm_remote_base_url, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    provider = APILLMProvider(settings, client=client)
    diagnostics = {"unit_id": "00000000-0000-0000-0000-000000000007"}
    arguments = dict(text="synthetic clause", filename="synthetic.pdf", anchor_metadata=anchor("clause"),
        piece_index=0, timeout_seconds=settings.kg_model_timeout_seconds, max_tokens=settings.kg_llm_max_tokens)
    try:
        result = extract_piece(provider, **arguments, diagnostics=diagnostics)
        assert result["entities"] == result["relationships"] == []
        success, = [r for r in caplog.records if getattr(r, "event", None) == "kg_llm_response_received"]
        assert success.unit_id == diagnostics["unit_id"] and success.piece_index == 0
        assert success.finish_reason == "stop" and success.effective_max_tokens == budget
        assert success.prompt_tokens == 4000 and success.completion_tokens == 8 and success.total_tokens == 4008
        assert _generation_diagnostics.get() is None
        snapshot = dict(diagnostics)
        provider.generate(LLMGenerateRequest.from_prompt("ordinary workflow"))
        assert diagnostics == snapshot  # No metadata leaking from a later non-KG request.
        assert not any(getattr(r, "event", None) == "llm_generation_failed" for r in caplog.records)
        with pytest.raises(BusinessError) as caught:
            extract_piece(provider, **arguments, diagnostics=diagnostics)
    finally:
        provider.close()
    assert caught.value.code == LLM_JSON_INVALID
    assert _generation_diagnostics.get() is None
    assert diagnostics["phase"] == "llm_generate"
    assert diagnostics["finish_reason"] == "length" and diagnostics["completion_tokens"] == budget
    assert [body["max_tokens"] for body in captured] == [budget, 1024, budget]
    assert "response_format" not in captured[1]
    for body in (captured[0], captured[2]):
        assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0.1
        assert not {"tools", "think", "stop", "max_output_tokens"}.intersection(body)
    record, = [r for r in caplog.records if getattr(r, "event", None) == "llm_generation_failed"]
    assert record.effective_max_tokens == record.completion_tokens == budget
    assert record.prompt_tokens == 4000 and record.total_tokens == 4000 + budget
    assert record.finish_reason == "length" and record.json_failure_kind == "decode_error"
    assert record.starts_with_object and not record.ends_with_object and not record.contains_fence


@pytest.mark.parametrize("metadata_case", ["none", "missing", "raising", "unsafe"])
def test_kg_metadata_capture_tolerates_missing_or_unsafe_fields(metadata_case):
    from app.llm.openai_chat_transport import capture_generation_diagnostics, _capture_response_metadata
    completion = FakeCompletion(content='{"entities":[],"relationships":[]}')
    if metadata_case == "none":
        completion.usage = None
    elif metadata_case == "missing":
        del completion.usage
    elif metadata_case == "raising":
        class Unreadable:
            def __getattr__(self, name):
                raise RuntimeError("SECRET_METADATA")
        completion = Unreadable()
    else:
        completion.usage = SimpleNamespace(prompt_tokens="SECRET_USAGE", completion_tokens=True, total_tokens=-1)
        completion.choices[0].finish_reason = "SECRET_FINISH_REASON"
    fields = {}
    with capture_generation_diagnostics(fields):
        _capture_response_metadata(completion, 8192)
    assert fields == dict(finish_reason=None, prompt_tokens=None, completion_tokens=None,
                          total_tokens=None, effective_max_tokens=8192)


def test_kg_metadata_capture_failure_and_nested_scope_do_not_change_generation(monkeypatch):
    from app.llm import openai_chat_transport as transport
    outer, inner = {}, {}
    completion = FakeCompletion(content='{"entities":[],"relationships":[]}')
    with transport.capture_generation_diagnostics(outer):
        with transport.capture_generation_diagnostics(inner):
            transport._capture_response_metadata(completion, 8192)
        transport._capture_response_metadata(completion, 4096)
    assert outer["effective_max_tokens"] == 4096 and inner["effective_max_tokens"] == 8192
    assert transport._generation_diagnostics.get() is None
    def broken(*args):
        raise RuntimeError("SECRET_METADATA")
    monkeypatch.setattr(transport, "_response_metadata", broken)
    provider = APILLMProvider(api_settings(llm_remote_supports_json_mode=True),
        client=FakeOpenAIClient(FakeCompletions(response=completion)))
    with transport.capture_generation_diagnostics({}):
        result = provider.generate(LLMGenerateRequest.from_prompt("synthetic", json_mode=True))
    assert result.text == '{"entities":[],"relationships":[]}' and not hasattr(result, "finish_reason")


def test_kg_valid_json_length_response_keeps_existing_success_semantics(caplog):
    from app.extraction.kg_extract import extract_piece
    from test_kg_v2_protocol_units import anchor
    completion = FakeCompletion(content='{"entities":[],"relationships":[]}')
    completion.choices[0].finish_reason = "length"
    completion.usage.completion_tokens = 8192
    provider = APILLMProvider(api_settings(llm_remote_supports_json_mode=True),
        client=FakeOpenAIClient(FakeCompletions(response=completion)))
    with caplog.at_level(logging.INFO):
        result = extract_piece(provider, text="SECRET_SOURCE", filename="synthetic.pdf", anchor_metadata=anchor(),
            piece_index=0, timeout_seconds=120, max_tokens=8192)
    assert result["entities"] == result["relationships"] == []
    record, = [r for r in caplog.records if getattr(r, "event", None) == "kg_llm_response_received"]
    assert record.finish_reason == "length" and record.completion_tokens == record.effective_max_tokens == 8192
    assert "SECRET_SOURCE" not in caplog.text


@pytest.mark.parametrize("metadata_case", ["none", "missing", "raising_usage", "raising_fields", "unsafe_values"])
def test_api_json_failure_diagnostics_tolerate_unavailable_metadata(metadata_case, caplog):
    class Unreadable:
        def __getattr__(self, name):
            raise RuntimeError("SECRET_METADATA")
    completion = FakeCompletion(content='{"entities":[')
    if metadata_case == "none":
        completion.usage = None
    elif metadata_case == "missing":
        del completion.usage
    elif metadata_case == "raising_usage":
        class BrokenCompletion(Unreadable):
            choices = completion.choices
        completion = BrokenCompletion()
    elif metadata_case == "raising_fields":
        completion.usage = Unreadable()
        completion.choices[0] = SimpleNamespace(message=completion.choices[0].message)
        class BrokenChoice(Unreadable):
            message = completion.choices[0].message
        completion.choices[0] = BrokenChoice()
    else:
        completion.usage = SimpleNamespace(prompt_tokens="SECRET_METADATA", completion_tokens=True, total_tokens=-1)
        completion.choices[0].finish_reason = "SECRET_METADATA"
    provider = APILLMProvider(api_settings(llm_remote_supports_json_mode=True),
        client=FakeOpenAIClient(FakeCompletions(response=completion)))
    with pytest.raises(BusinessError) as caught:
        provider.generate(LLMGenerateRequest.from_prompt("synthetic", json_mode=True))
    assert caught.value.code == LLM_JSON_INVALID
    record, = [r for r in caplog.records if getattr(r, "event", None) == "llm_generation_failed"]
    assert record.json_failure_kind == "decode_error" and record.finish_reason is None
    assert record.prompt_tokens is record.completion_tokens is record.total_tokens is None
    assert "SECRET_METADATA" not in repr(record.__dict__)


def test_api_json_failure_diagnostics_do_not_log_content_or_change_error_detail(caplog):
    prompt = "SECRET_PROMPT PDF_SOURCE Authorization: Bearer SECRET_API_KEY"
    content = '{"entities":[{"name":"SECRET_RESPONSE PDF_SOURCE"'
    completion = FakeCompletion(content=content)
    completion.choices[0].finish_reason = "length"
    provider = APILLMProvider(api_settings(llm_remote_supports_json_mode=True, llm_remote_api_key="SECRET_API_KEY"),
        client=FakeOpenAIClient(FakeCompletions(response=completion)))
    with pytest.raises(BusinessError) as caught:
        provider.generate(LLMGenerateRequest.from_prompt(prompt, json_mode=True))
    rendered = caplog.text + repr([r.__dict__ for r in caplog.records]) + repr(vars(caught.value))
    rendered += "".join(traceback.format_exception(caught.value))
    for secret in (prompt, content, "SECRET_PROMPT", "SECRET_RESPONSE", "PDF_SOURCE", "Authorization", "SECRET_API_KEY"):
        assert secret not in rendered
    assert caught.value.detail == {"field": "choices[0].message.content"}


def test_api_json_failure_diagnostics_filter_untrusted_decoder_message():
    from app.llm.openai_chat_transport import _build_json_failure_diagnostics
    diagnostics = _build_json_failure_diagnostics("broken", None, None, failure_kind="decode_error",
        decode_error_fields=("SECRET_DECODER_INPUT", 0, 1, 1))
    assert diagnostics["json_error_msg"] == "JSON decoding failed"
    assert "SECRET" not in repr(diagnostics)


def test_api_json_failure_diagnostics_helper_error_cannot_replace_primary_failure(monkeypatch):
    from app.llm import openai_chat_transport as transport
    def broken(*args, **kwargs):
        raise RuntimeError("SECRET_DIAGNOSTIC_FAILURE")
    monkeypatch.setattr(transport, "_build_json_failure_diagnostics", broken)
    provider = APILLMProvider(api_settings(llm_remote_supports_json_mode=True),
        client=FakeOpenAIClient(FakeCompletions(response=FakeCompletion(content="[]"))))
    with pytest.raises(BusinessError) as caught:
        provider.generate(LLMGenerateRequest.from_prompt("synthetic", json_mode=True))
    assert caught.value.code == LLM_JSON_INVALID and caught.value.__context__ is None


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
