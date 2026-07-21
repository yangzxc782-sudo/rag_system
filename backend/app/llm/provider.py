from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.errors import (
    LLM_CONFIG_INVALID,
    LLM_GENERATION_FAILED,
    LLM_REQUEST_INVALID,
    LLM_RESPONSE_INVALID,
    BusinessError,
)
from app.llm.messages import (
    LLMFunctionTool,
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

    def _legacy_prompt_messages(self) -> tuple[LLMMessage | None, LLMMessage]:
        if len(self.messages) == 1 and self.messages[0].role == "user":
            return None, self.messages[0]
        if (
            len(self.messages) == 2
            and self.messages[0].role == "system"
            and self.messages[1].role == "user"
        ):
            return self.messages[0], self.messages[1]
        _raise_request_invalid(
            "The legacy adapter only accepts system-to-user prompt messages.",
            detail={"field": "messages"},
        )

    @staticmethod
    def _single_text(message: LLMMessage) -> str:
        if len(message.content) != 1 or not isinstance(
            message.content[0], LLMTextContentPart
        ):
            _raise_request_invalid(
                "The legacy adapter only accepts one text part per message.",
                detail={"field": "messages"},
            )
        return message.content[0].text

    # Temporary read-only bridge for the M1B migration of openai_compatible.py.
    # These are deliberately properties, not formal request dataclass fields.
    @property
    def prompt(self) -> str:
        _, user_message = self._legacy_prompt_messages()
        return self._single_text(user_message)

    @property
    def system_prompt(self) -> str | None:
        system_message, _ = self._legacy_prompt_messages()
        return None if system_message is None else self._single_text(system_message)

    @property
    def response_format(self) -> dict[str, str] | None:
        return {"type": "json_object"} if self.json_mode else None


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
        raw: Any | None = None,
    ) -> None:
        if message is not None and text is not None:
            _raise_response_invalid(
                "LLM result cannot provide both message and legacy text.",
                detail={"field": "message"},
            )
        if raw is not None:
            _raise_response_invalid(
                "Raw LLM responses cannot enter the provider result contract.",
                detail={"field": "raw"},
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
