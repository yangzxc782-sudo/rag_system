from __future__ import annotations

from importlib import import_module
import json
from threading import Lock
from typing import Any, Callable, NoReturn

from pydantic import SecretStr

from app.core.errors import (
    LLM_AUTHENTICATION_FAILED,
    LLM_CONFIG_INVALID,
    LLM_EMPTY_CONTENT,
    LLM_GENERATION_FAILED,
    LLM_JSON_INVALID,
    LLM_MODEL_NOT_FOUND,
    LLM_PERMISSION_DENIED,
    LLM_RATE_LIMITED,
    LLM_REQUEST_INVALID,
    LLM_REQUEST_REJECTED,
    LLM_RESPONSE_INVALID,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    LLM_UPSTREAM_FAILED,
    BusinessError,
)
from app.llm.messages import LLMMessage, LLMTextContentPart
from app.llm.provider import (
    LLMGenerateRequest,
    LLMGenerateResult,
    LLMUsage,
)


class OpenAIChatTransport:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | SecretStr,
        timeout_seconds: float,
        send_think: bool = True,
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self._api_key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self.timeout_seconds = timeout_seconds
        self.send_think = send_think
        self._client = client
        self._client_factory = client_factory
        self._client_lock = Lock()
        self._closed = False

    def generate(
        self,
        request: LLMGenerateRequest,
        *,
        provider_name: str,
        model: str,
        default_temperature: float,
        default_max_tokens: int,
    ) -> LLMGenerateResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._serialize_text_messages(request),
            "temperature": (
                request.temperature
                if request.temperature is not None
                else default_temperature
            ),
            "max_tokens": (
                request.max_tokens
                if request.max_tokens is not None
                else default_max_tokens
            ),
        }
        if request.timeout_seconds is not None:
            payload["timeout"] = request.timeout_seconds
        if request.stop is not None:
            payload["stop"] = list(request.stop)
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.send_think and request.think is not None:
            payload["extra_body"] = {"think": request.think}

        mapped_error: BusinessError | None = None
        try:
            completion = self._get_client().chat.completions.create(**payload)
        except BusinessError:
            raise
        except Exception as exc:
            mapped_error = _map_llm_error(exc)
        if mapped_error is not None:
            raise mapped_error

        return self._parse_result(
            completion,
            provider_name=provider_name,
            model=model,
            json_mode=request.json_mode,
        )

    def close(self) -> None:
        with self._client_lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _get_client(self) -> Any:
        with self._client_lock:
            if self._closed:
                raise BusinessError(
                    LLM_UNAVAILABLE,
                    "LLM transport is closed.",
                    detail={"error_type": "TransportClosed"},
                    status_code=503,
                )
            if self._client is None:
                factory = self._client_factory or _default_openai_client_factory
                self._client = factory(
                    base_url=self.base_url,
                    api_key=self._api_key.get_secret_value(),
                    timeout=self.timeout_seconds,
                    max_retries=0,
                )
            return self._client

    @staticmethod
    def _serialize_text_messages(
        request: LLMGenerateRequest,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for message in request.messages:
            if (
                message.role not in {"system", "user", "assistant"}
                or len(message.content) != 1
                or not isinstance(message.content[0], LLMTextContentPart)
                or message.tool_calls
            ):
                raise BusinessError(
                    LLM_REQUEST_INVALID,
                    "OpenAI Chat text transport received a non-text message.",
                    detail={"field": "messages"},
                    status_code=400,
                )
            messages.append(
                {"role": message.role, "content": message.content[0].text}
            )
        return messages

    @staticmethod
    def _parse_result(
        completion: Any,
        *,
        provider_name: str,
        model: str,
        json_mode: bool,
    ) -> LLMGenerateResult:
        choices = getattr(completion, "choices", None)
        if not choices:
            _raise_response_invalid("LLM response did not contain choices.", "choices")
        message = getattr(choices[0], "message", None)
        if (
            message is None
            or getattr(message, "role", None) != "assistant"
            or getattr(message, "tool_calls", None)
        ):
            _raise_response_invalid(
                "LLM response contained an unsupported assistant message.",
                "choices[0].message",
            )
        content = getattr(message, "content", None)
        if content is None:
            _raise_empty_content(
                "LLM response did not contain generated text.",
                "choices[0].message.content",
            )
        if not isinstance(content, str):
            _raise_response_invalid(
                "LLM response contained unsupported structured content.",
                "choices[0].message.content",
                received_type=type(content).__name__,
            )
        if not content.strip():
            _raise_empty_content(
                "LLM response did not contain generated text.",
                "choices[0].message.content",
            )
        if json_mode:
            try:
                parsed_content = json.loads(content)
            except (TypeError, ValueError):
                _raise_json_invalid()
            if not isinstance(parsed_content, dict):
                _raise_json_invalid()

        usage_object = getattr(completion, "usage", None)
        usage = None
        if usage_object is not None:
            usage = LLMUsage(
                prompt_tokens=getattr(usage_object, "prompt_tokens", None),
                completion_tokens=getattr(usage_object, "completion_tokens", None),
                total_tokens=getattr(usage_object, "total_tokens", None),
            )
        request_id = getattr(completion, "id", None)
        if not isinstance(request_id, str) or not request_id:
            request_id = None
        return LLMGenerateResult(
            message=LLMMessage(
                role="assistant",
                content=(LLMTextContentPart(text=content),),
            ),
            provider=provider_name,
            model=model,
            usage=usage,
            request_id=request_id,
        )


def _default_openai_client_factory(
    *,
    base_url: str,
    api_key: str,
    timeout: float,
    max_retries: int,
) -> Any:
    try:
        openai_module = import_module("openai")
        openai_client = getattr(openai_module, "OpenAI")
    except Exception as exc:
        raise BusinessError(
            LLM_CONFIG_INVALID,
            "OpenAI Python client dependency is not available.",
            detail={"dependency": "openai", "error_type": type(exc).__name__},
            status_code=500,
        ) from exc
    return openai_client(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
    )


def _raise_response_invalid(
    message: str,
    field: str,
    *,
    received_type: str | None = None,
) -> NoReturn:
    detail = {"field": field}
    if received_type is not None:
        detail["received_type"] = received_type
    raise BusinessError(
        LLM_RESPONSE_INVALID,
        message,
        detail=detail,
        status_code=502,
    )


def _raise_empty_content(message: str, field: str) -> NoReturn:
    raise BusinessError(
        LLM_EMPTY_CONTENT,
        message,
        detail={"field": field},
        status_code=502,
    )


def _raise_json_invalid() -> NoReturn:
    raise BusinessError(
        LLM_JSON_INVALID,
        "LLM JSON response must contain a valid JSON object.",
        detail={"field": "choices[0].message.content"},
        status_code=502,
    ) from None


def _map_llm_error(exc: Exception) -> BusinessError:
    error_type = type(exc).__name__
    normalized_type = error_type.lower()
    if "timeout" in normalized_type:
        return BusinessError(
            LLM_TIMEOUT,
            "LLM request timed out.",
            detail={"source": "upstream_llm", "error_type": error_type},
            status_code=504,
        )
    if "connection" in normalized_type:
        return BusinessError(
            LLM_UNAVAILABLE,
            "LLM service is unavailable.",
            detail={"source": "upstream_llm", "error_type": error_type},
            status_code=503,
        )

    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    detail: dict[str, Any] = {
        "source": "upstream_llm",
        "error_type": error_type,
    }
    if isinstance(status_code, int):
        detail["upstream_status"] = status_code

    if status_code == 401:
        return BusinessError(
            LLM_AUTHENTICATION_FAILED,
            "LLM upstream authentication failed.",
            detail=detail,
            status_code=502,
        )
    if status_code == 403:
        return BusinessError(
            LLM_PERMISSION_DENIED,
            "LLM upstream permission was denied.",
            detail=detail,
            status_code=502,
        )
    if status_code == 404:
        return BusinessError(
            LLM_MODEL_NOT_FOUND,
            "LLM upstream model was not found.",
            detail=detail,
            status_code=502,
        )
    if status_code == 429:
        return BusinessError(
            LLM_RATE_LIMITED,
            "LLM upstream rate limit was reached.",
            detail={**detail, "retryable": True},
            status_code=429,
        )
    if isinstance(status_code, int) and status_code >= 500:
        return BusinessError(
            LLM_UPSTREAM_FAILED,
            "LLM upstream service failed.",
            detail=detail,
            status_code=502,
        )
    if isinstance(status_code, int) and 400 <= status_code < 500:
        return BusinessError(
            LLM_REQUEST_REJECTED,
            "LLM upstream rejected the request.",
            detail=detail,
            status_code=502,
        )
    if "responsevalidation" in normalized_type:
        return BusinessError(
            LLM_RESPONSE_INVALID,
            "LLM upstream response was invalid.",
            detail=detail,
            status_code=502,
        )
    return BusinessError(
        LLM_GENERATION_FAILED,
        "LLM generation failed.",
        detail={"source": "upstream_llm", "error_type": error_type},
        status_code=500,
    )
