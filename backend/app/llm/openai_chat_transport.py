from __future__ import annotations

from importlib import import_module
from threading import Lock
from typing import Any, Callable, NoReturn

from app.core.errors import (
    LLM_CONFIG_INVALID,
    LLM_GENERATION_FAILED,
    LLM_REQUEST_INVALID,
    LLM_RESPONSE_INVALID,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
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
        api_key: str,
        timeout_seconds: float,
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
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
        if request.think is not None:
            payload["extra_body"] = {"think": request.think}

        try:
            completion = self._get_client().chat.completions.create(**payload)
        except BusinessError:
            raise
        except Exception as exc:
            _raise_llm_error(exc)

        return self._parse_result(
            completion,
            provider_name=provider_name,
            model=model,
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
                    api_key=self.api_key,
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
    ) -> LLMGenerateResult:
        choices = getattr(completion, "choices", None)
        if not choices:
            _raise_response_invalid("LLM response did not contain choices.", "choices")
        message = getattr(choices[0], "message", None)
        if message is None or getattr(message, "tool_calls", None):
            _raise_response_invalid(
                "LLM response contained an unsupported assistant message.",
                "choices[0].message",
            )
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            _raise_response_invalid(
                "LLM response did not contain generated text.",
                "choices[0].message.content",
            )

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


def _raise_response_invalid(message: str, field: str) -> NoReturn:
    raise BusinessError(
        LLM_RESPONSE_INVALID,
        message,
        detail={"field": field},
        status_code=502,
    )


def _raise_llm_error(exc: Exception) -> NoReturn:
    error_type = type(exc).__name__
    normalized_type = error_type.lower()
    if "timeout" in normalized_type:
        raise BusinessError(
            LLM_TIMEOUT,
            "LLM request timed out.",
            detail={"error_type": error_type},
            status_code=504,
        ) from exc
    if "connection" in normalized_type:
        raise BusinessError(
            LLM_UNAVAILABLE,
            "LLM service is unavailable.",
            detail={"error_type": error_type},
            status_code=503,
        ) from exc
    raise BusinessError(
        LLM_GENERATION_FAILED,
        "LLM generation failed.",
        detail={"error_type": error_type},
        status_code=500,
    ) from exc
