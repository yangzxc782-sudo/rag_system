from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol

from app.core.config import get_settings
from app.core.errors import (
    LLM_CONFIG_INVALID,
    LLM_GENERATION_FAILED,
    LLM_PARAMETER_UNSUPPORTED,
    LLM_REQUEST_INVALID,
    LLM_RESPONSE_INVALID,
    BusinessError,
)
from app.llm.messages import (
    LLMFunctionTool,
    LLMImageURLContentPart,
    LLMMessage,
    LLMTextContentPart,
)


def _raise_request_invalid(
    message: str,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    raise BusinessError(
        LLM_REQUEST_INVALID,
        message,
        detail=detail,
        status_code=400,
    )


def _raise_response_invalid(
    message: str,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    raise BusinessError(
        LLM_RESPONSE_INVALID,
        message,
        detail=detail,
        status_code=502,
    )


class _UnsetType:
    __slots__ = ()


_UNSET = _UnsetType()


@dataclass(frozen=True, slots=True)
class LLMCapabilities:
    supports_json_mode: bool
    supports_think: bool
    supports_tools: bool
    supports_parallel_tool_calls: bool
    supports_image_input: bool

    def __post_init__(self) -> None:
        values = (
            self.supports_json_mode,
            self.supports_think,
            self.supports_tools,
            self.supports_parallel_tool_calls,
            self.supports_image_input,
        )
        if any(not isinstance(value, bool) for value in values):
            _raise_request_invalid(
                "LLM capability values must be booleans.",
                detail={"field": "capabilities"},
            )
        if self.supports_parallel_tool_calls and not self.supports_tools:
            _raise_request_invalid(
                "Parallel tool calls require tool support.",
                detail={"field": "supports_parallel_tool_calls"},
            )


@dataclass(frozen=True, slots=True, init=False)
class LLMGenerateRequest:
    messages: tuple[LLMMessage, ...]
    temperature: float | None = None
    max_tokens: int | None = None
    json_mode: bool = False
    think: bool | None = None
    think_required: bool = False
    timeout_seconds: float | None = None
    stop: tuple[str, ...] | None = None
    tools: tuple[LLMFunctionTool, ...] = ()
    parallel_tool_calls: bool | None = None

    def __init__(
        self,
        messages: tuple[LLMMessage, ...] | _UnsetType = _UNSET,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool | _UnsetType = _UNSET,
        think: bool | None = None,
        think_required: bool = False,
        timeout_seconds: float | None = None,
        stop: tuple[str, ...] | None = None,
        tools: tuple[LLMFunctionTool, ...] = (),
        parallel_tool_calls: bool | None = None,
        *,
        prompt: str | None | _UnsetType = _UNSET,
        system_prompt: str | None | _UnsetType = _UNSET,
        response_format: dict[str, Any] | None | _UnsetType = _UNSET,
    ) -> None:
        legacy_input_supplied = any(
            value is not _UNSET for value in (prompt, system_prompt, response_format)
        )
        messages_supplied = messages is not _UNSET

        if messages_supplied and legacy_input_supplied:
            _raise_request_invalid(
                "LLM messages cannot be combined with legacy prompt inputs.",
                detail={"field": "messages"},
            )

        resolved_messages: object
        resolved_json_mode: object
        if legacy_input_supplied:
            if prompt is _UNSET:
                _raise_request_invalid(
                    "Legacy LLM request construction requires prompt.",
                    detail={"field": "prompt"},
                )
            if response_format is not _UNSET and json_mode is not _UNSET:
                _raise_request_invalid(
                    "Legacy response_format cannot be combined with json_mode.",
                    detail={"field": "json_mode"},
                )

            # Temporary M1A bridge: existing RAG and knowledge extraction still
            # construct prompt-based requests. M3 must migrate those services to
            # from_prompt() and then delete these legacy constructor parameters.
            legacy_messages: list[LLMMessage] = []
            if system_prompt is not _UNSET and system_prompt is not None:
                legacy_messages.append(
                    LLMMessage(
                        role="system",
                        content=(LLMTextContentPart(text=system_prompt),),
                    )
                )
            legacy_messages.append(
                LLMMessage(
                    role="user",
                    content=(LLMTextContentPart(text=prompt),),
                )
            )
            resolved_messages = tuple(legacy_messages)
            resolved_json_mode = self._legacy_json_mode(response_format, json_mode)
        else:
            resolved_messages = () if messages is _UNSET else messages
            resolved_json_mode = False if json_mode is _UNSET else json_mode

        object.__setattr__(self, "messages", resolved_messages)
        object.__setattr__(self, "temperature", temperature)
        object.__setattr__(self, "max_tokens", max_tokens)
        object.__setattr__(self, "json_mode", resolved_json_mode)
        object.__setattr__(self, "think", think)
        object.__setattr__(self, "think_required", think_required)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        object.__setattr__(self, "stop", stop)
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "parallel_tool_calls", parallel_tool_calls)
        self.__post_init__()

    @staticmethod
    def _legacy_json_mode(
        response_format: dict[str, Any] | None | _UnsetType,
        json_mode: bool | _UnsetType,
    ) -> bool | _UnsetType:
        if response_format is _UNSET:
            return False if json_mode is _UNSET else json_mode
        if response_format is None:
            return False
        if isinstance(response_format, dict) and response_format == {
            "type": "json_object"
        }:
            return True
        _raise_request_invalid(
            "Legacy LLM response_format is unsupported.",
            detail={"field": "response_format"},
        )

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple) or not self.messages:
            _raise_request_invalid(
                "LLM request must contain at least one message.",
                detail={"field": "messages"},
            )
        if any(not isinstance(message, LLMMessage) for message in self.messages):
            _raise_request_invalid(
                "LLM request messages are invalid.",
                detail={"field": "messages"},
            )
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or self.temperature < 0
            or self.temperature > 2
        ):
            _raise_request_invalid(
                "LLM temperature must be between 0 and 2.",
                detail={"field": "temperature"},
            )
        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens <= 0
        ):
            _raise_request_invalid(
                "LLM max_tokens must be greater than 0.",
                detail={"field": "max_tokens"},
            )
        if not isinstance(self.json_mode, bool):
            _raise_request_invalid(
                "LLM json_mode must be a boolean.",
                detail={"field": "json_mode"},
            )
        if self.think is not None and not isinstance(self.think, bool):
            _raise_request_invalid(
                "LLM think must be a boolean or null.",
                detail={"field": "think"},
            )
        if not isinstance(self.think_required, bool):
            _raise_request_invalid(
                "LLM think_required must be a boolean.",
                detail={"field": "think_required"},
            )
        if self.think_required and self.think is None:
            _raise_request_invalid(
                "LLM think_required requires an explicit think value.",
                detail={"field": "think_required"},
            )
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            _raise_request_invalid(
                "LLM timeout_seconds must be greater than 0.",
                detail={"field": "timeout_seconds"},
            )
        if self.stop is not None and (
            not isinstance(self.stop, tuple)
            or not self.stop
            or any(not isinstance(value, str) or not value for value in self.stop)
        ):
            _raise_request_invalid(
                "LLM stop must contain non-empty strings.",
                detail={"field": "stop"},
            )
        if not isinstance(self.tools, tuple) or any(
            not isinstance(tool, LLMFunctionTool) for tool in self.tools
        ):
            _raise_request_invalid(
                "LLM tools are invalid.",
                detail={"field": "tools"},
            )
        if self.parallel_tool_calls is not None and not isinstance(
            self.parallel_tool_calls, bool
        ):
            _raise_request_invalid(
                "LLM parallel_tool_calls must be a boolean or null.",
                detail={"field": "parallel_tool_calls"},
            )

        self._validate_tool_linkage()

    @classmethod
    def from_prompt(
        cls,
        prompt: str,
        system_prompt: str | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        think: bool | None = None,
        think_required: bool = False,
        timeout_seconds: float | None = None,
        stop: tuple[str, ...] | None = None,
    ) -> LLMGenerateRequest:
        messages: list[LLMMessage] = []
        if system_prompt is not None:
            messages.append(
                LLMMessage(
                    role="system",
                    content=(LLMTextContentPart(text=system_prompt),),
                )
            )
        messages.append(
            LLMMessage(role="user", content=(LLMTextContentPart(text=prompt),))
        )
        return cls(
            messages=tuple(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            think=think,
            think_required=think_required,
            timeout_seconds=timeout_seconds,
            stop=stop,
        )

    def _validate_tool_linkage(self) -> None:
        seen_tool_call_ids: set[str] = set()
        answered_tool_call_ids: set[str] = set()

        for message_index, message in enumerate(self.messages):
            for call in message.tool_calls:
                if call.id in seen_tool_call_ids:
                    _raise_request_invalid(
                        "LLM tool call IDs must be globally unique.",
                        detail={"field": "tool_calls", "message_index": message_index},
                    )
                seen_tool_call_ids.add(call.id)

            if message.role != "tool":
                continue
            if message.tool_call_id not in seen_tool_call_ids:
                _raise_request_invalid(
                    "LLM tool message must reference an earlier assistant tool call.",
                    detail={"field": "tool_call_id", "message_index": message_index},
                )
            if message.tool_call_id in answered_tool_call_ids:
                _raise_request_invalid(
                    "An LLM tool call can have only one response.",
                    detail={"field": "tool_call_id", "message_index": message_index},
                )
            answered_tool_call_ids.add(message.tool_call_id)

@dataclass(frozen=True, slots=True)
class LLMUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True, init=False)
class LLMGenerateResult:
    message: LLMMessage
    provider: str
    model: str
    usage: LLMUsage | None
    request_id: str | None

    def __init__(
        self,
        message: LLMMessage | None = None,
        provider: str = "",
        model: str = "",
        usage: LLMUsage | None = None,
        request_id: str | None = None,
        *,
        text: str | None = None,
    ) -> None:
        if message is not None and text is not None:
            _raise_response_invalid(
                "LLM result cannot provide both message and legacy text.",
                detail={"field": "message"},
            )
        if message is None:
            if text is None:
                _raise_response_invalid(
                    "LLM result must contain an assistant message.",
                    detail={"field": "message"},
                )
            try:
                message = LLMMessage(
                    role="assistant",
                    content=(LLMTextContentPart(text=text),),
                )
            except BusinessError:
                # Temporary compatibility for pre-M3 service fakes that still
                # construct text-first results and expect the legacy error code.
                raise BusinessError(
                    LLM_GENERATION_FAILED,
                    "LLM response did not contain generated text.",
                    detail={"field": "message.content"},
                    status_code=500,
                )
        if not isinstance(message, LLMMessage) or message.role != "assistant":
            _raise_response_invalid(
                "LLM result message must have the assistant role.",
                detail={"field": "message.role"},
            )
        if not isinstance(provider, str) or not provider.strip():
            _raise_response_invalid(
                "LLM result provider must not be empty.",
                detail={"field": "provider"},
            )
        if not isinstance(model, str) or not model.strip():
            _raise_response_invalid(
                "LLM result model must not be empty.",
                detail={"field": "model"},
            )
        if usage is not None and not isinstance(usage, LLMUsage):
            _raise_response_invalid(
                "LLM result usage is invalid.",
                detail={"field": "usage"},
            )
        if request_id is not None and not isinstance(request_id, str):
            _raise_response_invalid(
                "LLM result request_id is invalid.",
                detail={"field": "request_id"},
            )

        object.__setattr__(self, "message", message)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "usage", usage)
        object.__setattr__(self, "request_id", request_id)

    @property
    def text(self) -> str:
        text_parts = [
            part for part in self.message.content if isinstance(part, LLMTextContentPart)
        ]
        if len(text_parts) != 1:
            _raise_response_invalid(
                "LLM result must contain exactly one assistant text part.",
                detail={"field": "message.content"},
            )
        return text_parts[0].text


class LLMProvider(Protocol):
    provider_name: str
    capabilities: LLMCapabilities

    def generate(self, request: LLMGenerateRequest) -> LLMGenerateResult:
        ...

    def close(self) -> None:
        ...


def _raise_parameter_unsupported(
    provider_name: str,
    parameter: str,
    required_capability: str,
) -> None:
    raise BusinessError(
        LLM_PARAMETER_UNSUPPORTED,
        "LLM provider does not support a requested parameter.",
        detail={
            "provider": provider_name,
            "parameter": parameter,
            "required_capability": required_capability,
        },
        status_code=400,
    )


def validate_llm_request_capabilities(
    request: LLMGenerateRequest,
    *,
    provider_name: str,
    capabilities: LLMCapabilities,
) -> None:
    if request.json_mode and not capabilities.supports_json_mode:
        _raise_parameter_unsupported(
            provider_name, "json_mode", "supports_json_mode"
        )
    if request.think_required and not capabilities.supports_think:
        _raise_parameter_unsupported(provider_name, "think", "supports_think")
    if request.tools and not capabilities.supports_tools:
        _raise_parameter_unsupported(provider_name, "tools", "supports_tools")
    if any(message.role == "tool" or message.tool_calls for message in request.messages):
        if not capabilities.supports_tools:
            _raise_parameter_unsupported(provider_name, "tools", "supports_tools")
    if request.parallel_tool_calls is True and not capabilities.supports_parallel_tool_calls:
        _raise_parameter_unsupported(
            provider_name,
            "parallel_tool_calls",
            "supports_parallel_tool_calls",
        )
    if any(len(message.tool_calls) > 1 for message in request.messages):
        if not capabilities.supports_parallel_tool_calls:
            _raise_parameter_unsupported(
                provider_name,
                "parallel_tool_calls",
                "supports_parallel_tool_calls",
            )
    if any(
        isinstance(part, LLMImageURLContentPart)
        for message in request.messages
        for part in message.content
    ) and not capabilities.supports_image_input:
        _raise_parameter_unsupported(
            provider_name, "image_input", "supports_image_input"
        )


_provider_cache_lock = Lock()
_provider_cache: LLMProvider | None = None


def build_llm_provider(
    settings: Any,
    *,
    client: Any | None = None,
) -> LLMProvider:
    from app.llm.configuration import validate_active_llm_configuration

    metadata = validate_active_llm_configuration(settings)
    if metadata.provider == "local":
        from app.llm.local import LocalLLMProvider

        return LocalLLMProvider(settings, client=client)
    raise BusinessError(
        LLM_CONFIG_INVALID,
        "API LLM provider is not available in M1B.",
        detail={"field": "llm_provider"},
        status_code=400,
    )


def get_llm_provider(
    settings: Any | None = None,
    *,
    client: Any | None = None,
) -> LLMProvider:
    # Temporary M3 bridge: current services still pass Settings explicitly.
    # That path stays uncached until M3 migrates services to the formal no-arg API.
    if settings is not None or client is not None:
        return build_llm_provider(settings or get_settings(), client=client)

    global _provider_cache
    with _provider_cache_lock:
        if _provider_cache is None:
            _provider_cache = build_llm_provider(get_settings())
        return _provider_cache


def clear_llm_provider_cache() -> None:
    global _provider_cache
    with _provider_cache_lock:
        provider = _provider_cache
        _provider_cache = None
    if provider is not None:
        provider.close()
