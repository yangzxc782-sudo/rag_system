from __future__ import annotations

from importlib import import_module
import json
import logging
import re
from threading import Lock
from time import perf_counter
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


logger = logging.getLogger(__name__)
_SAFE_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class OpenAIChatTransport:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | SecretStr,
        timeout_seconds: float,
        send_think: bool = True,
        http_client_trust_env: bool | None = None,
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self._api_key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self.timeout_seconds = timeout_seconds
        self.send_think = send_think
        self._http_client_trust_env = http_client_trust_env
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

        started_at = perf_counter()
        mapped_error: BusinessError | None = None
        try:
            completion = self._get_client().chat.completions.create(**payload)
        except BusinessError as error:
            _log_generation_failure(
                error,
                provider_name=provider_name,
                model=model,
                started_at=started_at,
            )
            raise
        except Exception as exc:
            mapped_error = _map_llm_error(exc)
        if mapped_error is not None:
            _log_generation_failure(
                mapped_error,
                provider_name=provider_name,
                model=model,
                started_at=started_at,
            )
            raise mapped_error

        try:
            result = self._parse_result(
                completion,
                provider_name=provider_name,
                model=model,
                json_mode=request.json_mode,
            )
        except BusinessError as error:
            _log_generation_failure(
                error,
                provider_name=provider_name,
                model=model,
                started_at=started_at,
            )
            raise

        _log_generation_success(result, started_at=started_at)
        return result

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
                factory_kwargs: dict[str, Any] = {
                    "base_url": self.base_url,
                    "api_key": self._api_key.get_secret_value(),
                    "timeout": self.timeout_seconds,
                    "max_retries": 0,
                }
                if (
                    self._client_factory is None
                    and self._http_client_trust_env is not None
                ):
                    factory_kwargs["http_client_trust_env"] = (
                        self._http_client_trust_env
                    )
                self._client = factory(
                    **factory_kwargs,
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
            parsed_content: Any = None
            json_parse_failed = False
            try:
                parsed_content = json.loads(content)
            except (TypeError, ValueError):
                json_parse_failed = True
            if json_parse_failed or not isinstance(parsed_content, dict):
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
    http_client_trust_env: bool | None = None,
) -> Any:
    import_error_type: str | None = None
    openai_client: Any = None
    try:
        openai_module = import_module("openai")
        openai_client = getattr(openai_module, "OpenAI")
    except Exception as exc:
        import_error_type = type(exc).__name__

    if import_error_type is not None:
        raise BusinessError(
            LLM_CONFIG_INVALID,
            "OpenAI Python client dependency is not available.",
            detail={"dependency": "openai", "error_type": import_error_type},
            status_code=500,
        )
    http_client: Any | None = None
    try:
        client_kwargs: dict[str, Any] = {
            "base_url": base_url,
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": max_retries,
        }
        if http_client_trust_env is not None:
            default_http_client = getattr(openai_module, "DefaultHttpxClient")
            http_client = default_http_client(
                trust_env=http_client_trust_env
            )
            client_kwargs["http_client"] = http_client
        return openai_client(**client_kwargs)
    except Exception:
        if http_client is not None:
            http_client.close()
        raise


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
    )


def _latency_ms(started_at: float) -> int:
    return max(0, int((perf_counter() - started_at) * 1000))


def _safe_request_id(request_id: str | None) -> str | None:
    if request_id is None or _SAFE_REQUEST_ID_PATTERN.fullmatch(request_id) is None:
        return None
    return request_id


def _log_generation_success(
    result: LLMGenerateResult,
    *,
    started_at: float,
) -> None:
    usage = result.usage
    logger.info(
        "LLM generation completed.",
        extra={
            "event": "llm_generation_completed",
            "provider": result.provider,
            "model": result.model,
            "operation": "chat.completions",
            "latency_ms": _latency_ms(started_at),
            "request_id": _safe_request_id(result.request_id),
            "prompt_tokens": usage.prompt_tokens if usage is not None else None,
            "completion_tokens": usage.completion_tokens if usage is not None else None,
            "total_tokens": usage.total_tokens if usage is not None else None,
        },
    )


def _log_generation_failure(
    error: BusinessError,
    *,
    provider_name: str,
    model: str,
    started_at: float,
) -> None:
    detail = error.detail if isinstance(error.detail, dict) else {}
    upstream_status = detail.get("upstream_status")
    if not isinstance(upstream_status, int):
        upstream_status = None
    retryable = detail.get("retryable") is True
    logger.warning(
        "LLM generation failed.",
        extra={
            "event": "llm_generation_failed",
            "provider": provider_name,
            "model": model,
            "operation": "chat.completions",
            "latency_ms": _latency_ms(started_at),
            "error_code": error.code,
            "upstream_status": upstream_status,
            "retryable": retryable,
        },
    )


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
