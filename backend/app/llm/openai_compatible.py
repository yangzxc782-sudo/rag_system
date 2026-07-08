from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, NoReturn

from app.core.errors import (
    LLM_CONFIG_INVALID,
    LLM_GENERATION_FAILED,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    BusinessError,
)
from app.llm.provider import LLMGenerateRequest, LLMGenerateResult


OPENAI_COMPATIBLE_PROVIDER = "openai_compatible"


class OpenAICompatibleLLMProvider:
    provider_name = OPENAI_COMPATIBLE_PROVIDER

    def __init__(
        self,
        settings: Any,
        *,
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = str(getattr(settings, "llm_base_url", "")).strip()
        self.api_key = str(getattr(settings, "llm_api_key", "")).strip()
        self.model = str(getattr(settings, "llm_model", "")).strip()
        self.temperature = float(getattr(settings, "llm_temperature", 0.2))
        self.max_tokens = int(getattr(settings, "llm_max_tokens", 2048))
        self.timeout_seconds = int(getattr(settings, "llm_timeout_seconds", 120))
        self._client = client
        self._client_factory = client_factory
        self._validate_config()

    def generate(self, request: LLMGenerateRequest) -> LLMGenerateResult:
        messages = _build_messages(request)
        temperature = request.temperature if request.temperature is not None else self.temperature
        max_tokens = request.max_tokens if request.max_tokens is not None else self.max_tokens
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        if request.response_format is not None:
            payload["response_format"] = request.response_format

        extra_body: dict[str, Any] = {}
        if request.think is not None:
            extra_body["think"] = request.think

        if extra_body:
            payload["extra_body"] = extra_body

        try:
            completion = self._get_client().chat.completions.create(**payload)
        except Exception as exc:
            _raise_llm_error(exc)

        text = _extract_text(completion)
        return LLMGenerateResult(
            text=text,
            provider=self.provider_name,
            model=self.model,
            raw=None,
        )

    def _validate_config(self) -> None:
        if not self.base_url:
            raise BusinessError(
                LLM_CONFIG_INVALID,
                "LLM base_url must not be empty.",
                detail={"field": "llm_base_url"},
                status_code=400,
            )
        if not self.api_key:
            raise BusinessError(
                LLM_CONFIG_INVALID,
                "LLM api_key must not be empty.",
                detail={"field": "llm_api_key"},
                status_code=400,
            )
        if not self.model:
            raise BusinessError(
                LLM_CONFIG_INVALID,
                "LLM model must not be empty.",
                detail={"field": "llm_model"},
                status_code=400,
            )
        if self.timeout_seconds <= 0:
            raise BusinessError(
                LLM_CONFIG_INVALID,
                "LLM timeout must be greater than 0.",
                detail={"field": "llm_timeout_seconds", "value": self.timeout_seconds},
                status_code=400,
            )
        if self.temperature < 0 or self.temperature > 2:
            raise BusinessError(
                LLM_CONFIG_INVALID,
                "LLM temperature must be between 0 and 2.",
                detail={"field": "llm_temperature", "value": self.temperature},
                status_code=400,
            )
        if self.max_tokens <= 0:
            raise BusinessError(
                LLM_CONFIG_INVALID,
                "LLM max_tokens must be greater than 0.",
                detail={"field": "llm_max_tokens", "value": self.max_tokens},
                status_code=400,
            )

    def _get_client(self) -> Any:
        if self._client is None:
            factory = self._client_factory or _default_openai_client_factory
            self._client = factory(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout_seconds,
            )
        return self._client


def _default_openai_client_factory(*, base_url: str, api_key: str, timeout: int) -> Any:
    try:
        openai_module = import_module("openai")
        openai_client = getattr(openai_module, "OpenAI")
    except Exception as exc:
        raise BusinessError(
            LLM_CONFIG_INVALID,
            "OpenAI Python client dependency is not available.",
            detail={"dependency": "openai", "error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc

    return openai_client(base_url=base_url, api_key=api_key, timeout=timeout)


def _build_messages(request: LLMGenerateRequest) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if request.system_prompt is not None and request.system_prompt.strip():
        messages.append({"role": "system", "content": request.system_prompt})
    messages.append({"role": "user", "content": request.prompt})
    return messages


def _extract_text(completion: Any) -> str:
    choices = getattr(completion, "choices", None)
    if not choices:
        raise BusinessError(
            LLM_GENERATION_FAILED,
            "LLM response did not contain choices.",
            detail={"field": "choices"},
            status_code=500,
        )

    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise BusinessError(
            LLM_GENERATION_FAILED,
            "LLM response did not contain generated text.",
            detail={"field": "choices[0].message.content"},
            status_code=500,
        )
    return content


def _raise_llm_error(exc: Exception) -> NoReturn:
    error_type = exc.__class__.__name__
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
        detail={"error_type": error_type,
                "error_message": str(exc),
        },
        status_code=500,
    ) from exc
