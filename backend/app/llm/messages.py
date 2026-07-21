from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from typing import Any, Literal, TypeAlias
from urllib.parse import urlsplit

from app.core.errors import LLM_REQUEST_INVALID, BusinessError


LLMRole: TypeAlias = Literal["system", "user", "assistant", "tool"]
LLMImageDetail: TypeAlias = Literal["auto", "low", "high"]


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


def _require_non_empty_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _raise_request_invalid(
            "LLM text value must not be empty.",
            detail={"field": field_name},
        )
    return value


@dataclass(frozen=True, slots=True)
class LLMTextContentPart:
    text: str
    type: Literal["text"] = field(default="text", init=False)

    def __post_init__(self) -> None:
        _require_non_empty_text(self.text, field_name="text")


@dataclass(frozen=True, slots=True)
class LLMImageURLContentPart:
    image_url: str
    detail: LLMImageDetail = "auto"
    type: Literal["image_url"] = field(default="image_url", init=False)

    def __post_init__(self) -> None:
        image_url = _require_non_empty_text(self.image_url, field_name="image_url")
        if self.detail not in {"auto", "low", "high"}:
            _raise_request_invalid(
                "LLM image detail is invalid.",
                detail={"field": "detail"},
            )

        parsed = None
        try:
            parsed = urlsplit(image_url)
        except ValueError:
            pass
        if (
            parsed is None
            or parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
        ):
            _raise_request_invalid(
                "LLM image URL must be an absolute HTTP(S) URL.",
                detail={"field": "image_url"},
            )


LLMContentPart: TypeAlias = LLMTextContentPart | LLMImageURLContentPart


@dataclass(frozen=True, slots=True)
class LLMFunctionCall:
    name: str
    arguments: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.name, field_name="function.name")
        if not isinstance(self.arguments, str):
            _raise_request_invalid(
                "LLM function call arguments must be a string.",
                detail={"field": "function.arguments"},
            )


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    id: str
    function: LLMFunctionCall
    type: Literal["function"] = field(default="function", init=False)

    def __post_init__(self) -> None:
        _require_non_empty_text(self.id, field_name="tool_call.id")
        if not isinstance(self.function, LLMFunctionCall):
            _raise_request_invalid(
                "LLM tool call function is invalid.",
                detail={"field": "tool_call.function"},
            )


@dataclass(frozen=True, slots=True, init=False)
class LLMFunctionTool:
    name: str
    description: str | None
    _parameters_json: str = field(repr=False)

    def __init__(
        self,
        *,
        name: str,
        parameters: Mapping[str, Any],
        description: str | None = None,
    ) -> None:
        _require_non_empty_text(name, field_name="tool.name")
        if description is not None and not isinstance(description, str):
            _raise_request_invalid(
                "LLM function tool description must be a string or null.",
                detail={"field": "tool.description"},
            )
        if not isinstance(parameters, Mapping):
            _raise_request_invalid(
                "LLM function tool parameters must be a JSON object.",
                detail={"field": "tool.parameters"},
            )

        parameters_object = dict(parameters)
        serialization_error_type: str | None = None
        copied_parameters: Any = None
        parameters_json = ""
        try:
            parameters_json = json.dumps(
                parameters_object,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            copied_parameters = json.loads(parameters_json)
        except (TypeError, ValueError) as exc:
            serialization_error_type = type(exc).__name__

        if serialization_error_type is not None:
            _raise_request_invalid(
                "LLM function tool parameters must be strict JSON.",
                detail={
                    "field": "tool.parameters",
                    "error_type": serialization_error_type,
                },
            )

        if not isinstance(copied_parameters, dict):
            _raise_request_invalid(
                "LLM function tool parameters must be a JSON object.",
                detail={"field": "tool.parameters"},
            )

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "_parameters_json", parameters_json)

    @property
    def parameters(self) -> dict[str, Any]:
        return json.loads(self._parameters_json)


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: LLMRole
    content: tuple[LLMContentPart, ...] = ()
    tool_calls: tuple[LLMToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            _raise_request_invalid(
                "LLM message role is invalid.",
                detail={"field": "role"},
            )
        if not isinstance(self.content, tuple) or any(
            not isinstance(part, (LLMTextContentPart, LLMImageURLContentPart))
            for part in self.content
        ):
            _raise_request_invalid(
                "LLM message content is invalid.",
                detail={"field": "content"},
            )
        if not isinstance(self.tool_calls, tuple) or any(
            not isinstance(call, LLMToolCall) for call in self.tool_calls
        ):
            _raise_request_invalid(
                "LLM message tool calls are invalid.",
                detail={"field": "tool_calls"},
            )

        if self.role == "system":
            self._validate_single_text_role("system")
            return

        if self.role == "user":
            if self.tool_calls or self.tool_call_id is not None or not self.content:
                _raise_request_invalid(
                    "LLM user message fields are invalid.",
                    detail={"role": "user"},
                )
            if len(self.content) > 1 and all(
                isinstance(part, LLMTextContentPart) for part in self.content
            ):
                _raise_request_invalid(
                    "LLM user message cannot contain multiple text-only parts.",
                    detail={"role": "user", "field": "content"},
                )
            return

        if self.role == "assistant":
            if self.tool_call_id is not None:
                _raise_request_invalid(
                    "LLM assistant message cannot have a tool_call_id.",
                    detail={"role": "assistant", "field": "tool_call_id"},
                )
            if any(not isinstance(part, LLMTextContentPart) for part in self.content):
                _raise_request_invalid(
                    "LLM assistant message only supports text content.",
                    detail={"role": "assistant", "field": "content"},
                )
            if len(self.content) > 1:
                _raise_request_invalid(
                    "LLM assistant message cannot contain multiple text parts.",
                    detail={"role": "assistant", "field": "content"},
                )
            if not self.content and not self.tool_calls:
                _raise_request_invalid(
                    "LLM assistant message must contain text or tool calls.",
                    detail={"role": "assistant"},
                )
            return

        self._validate_single_text_role("tool", require_tool_call_id=True)

    def _validate_single_text_role(
        self,
        role: Literal["system", "tool"],
        *,
        require_tool_call_id: bool = False,
    ) -> None:
        if (
            len(self.content) != 1
            or not isinstance(self.content[0], LLMTextContentPart)
            or self.tool_calls
        ):
            _raise_request_invalid(
                f"LLM {role} message must contain exactly one text part.",
                detail={"role": role, "field": "content"},
            )

        if require_tool_call_id:
            _require_non_empty_text(self.tool_call_id, field_name="tool_call_id")
        elif self.tool_call_id is not None:
            _raise_request_invalid(
                f"LLM {role} message cannot have a tool_call_id.",
                detail={"role": role, "field": "tool_call_id"},
            )
