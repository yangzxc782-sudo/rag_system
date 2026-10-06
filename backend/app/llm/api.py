from __future__ import annotations

from typing import Any, Callable

from pydantic import SecretStr

from app.core.errors import LLM_CONFIG_INVALID, BusinessError
from app.llm.configuration import validate_active_llm_configuration
from app.llm.openai_chat_transport import OpenAIChatTransport
from app.llm.provider import (
    LLMCapabilities,
    LLMGenerateRequest,
    LLMGenerateResult,
    validate_llm_request_capabilities,
)


class APILLMProvider:
    provider_name = "api"

    def __init__(
        self,
        settings: Any,
        *,
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        metadata = validate_active_llm_configuration(settings)
        if metadata.provider != "api":
            raise BusinessError(
                LLM_CONFIG_INVALID,
                "API LLM provider requires active API configuration.",
                detail={"field": "llm_provider"},
                status_code=400,
            )

        self.model = metadata.model
        self.temperature = float(getattr(settings, "llm_temperature"))
        self.max_tokens = int(getattr(settings, "llm_max_tokens"))
        self.capabilities = LLMCapabilities(
            supports_json_mode=bool(
                getattr(settings, "llm_remote_supports_json_mode", False)
            ),
            supports_think=False,
            supports_tools=bool(getattr(settings, "llm_remote_supports_tools", False)),
            supports_parallel_tool_calls=False,
            supports_image_input=False,
        )

        api_key = getattr(settings, "llm_remote_api_key")
        if not isinstance(api_key, SecretStr):
            api_key = SecretStr(str(api_key))
        self._transport = OpenAIChatTransport(
            base_url=str(getattr(settings, "llm_remote_base_url")).strip(),
            api_key=api_key,
            timeout_seconds=float(
                getattr(settings, "llm_remote_timeout_seconds")
            ),
            send_think=False,
            client=client,
            client_factory=client_factory,
        )

    def generate(self, request: LLMGenerateRequest) -> LLMGenerateResult:
        validate_llm_request_capabilities(
            request,
            provider_name=self.provider_name,
            capabilities=self.capabilities,
        )
        return self._transport.generate(
            request,
            provider_name=self.provider_name,
            model=self.model,
            default_temperature=self.temperature,
            default_max_tokens=self.max_tokens,
        )

    def close(self) -> None:
        self._transport.close()
