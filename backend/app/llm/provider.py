from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.errors import LLM_CONFIG_INVALID, BusinessError


@dataclass(frozen=True)
class LLMGenerateRequest:
    prompt: str
    system_prompt: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: dict[str, Any] | None = None
    think: bool | None = None


@dataclass(frozen=True)
class LLMGenerateResult:
    text: str
    provider: str
    model: str
    raw: Any | None = None


class LLMProvider(Protocol):
    provider_name: str

    def generate(self, request: LLMGenerateRequest) -> LLMGenerateResult:
        ...


def get_llm_provider(settings: Any, *, client: Any | None = None) -> LLMProvider:
    provider = str(getattr(settings, "llm_provider", "")).strip().lower()

    if provider == "openai_compatible":
        from app.llm.openai_compatible import OpenAICompatibleLLMProvider

        return OpenAICompatibleLLMProvider(settings, client=client)

    raise BusinessError(
        LLM_CONFIG_INVALID,
        "Unsupported LLM provider.",
        detail={"llm_provider": getattr(settings, "llm_provider", None)},
        status_code=400,
    )
