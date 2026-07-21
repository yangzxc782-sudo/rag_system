from __future__ import annotations

from dataclasses import fields
import inspect
import math
import traceback

import pytest

from app.core.errors import (
    LLM_REQUEST_INVALID,
    LLM_RESPONSE_INVALID,
    BusinessError,
)
from app.llm import (
    LLMCapabilities,
    LLMFunctionCall,
    LLMFunctionTool,
    LLMGenerateRequest,
    LLMGenerateResult,
    LLMImageURLContentPart,
    LLMMessage,
    LLMTextContentPart,
    LLMToolCall,
)


def text(value: str) -> LLMTextContentPart:
    return LLMTextContentPart(text=value)


def message(role: str, value: str) -> LLMMessage:
    return LLMMessage(role=role, content=(text(value),))  # type: ignore[arg-type]


def tool_call(call_id: str = "call-1") -> LLMToolCall:
    return LLMToolCall(
        id=call_id,
        function=LLMFunctionCall(name="lookup", arguments='{"query":"砂型"}'),
    )


def assert_request_invalid(callable_: object) -> None:
    with pytest.raises(BusinessError) as exc_info:
        callable_()  # type: ignore[operator]

    assert exc_info.value.code == LLM_REQUEST_INVALID
    assert exc_info.value.status_code == 400


def test_system_message_requires_exactly_one_text_part() -> None:
    system = message("system", "只回答铸造问题")

    assert system.content == (text("只回答铸造问题"),)
    assert_request_invalid(lambda: LLMMessage(role="system"))
    assert_request_invalid(
        lambda: LLMMessage(role="system", content=(text("一"), text("二")))
    )


def test_user_text_message_requires_exactly_one_text_part() -> None:
    user = message("user", "什么是砂型？")

    assert user.content == (text("什么是砂型？"),)
    assert_request_invalid(lambda: LLMMessage(role="user"))


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_text_content_part_rejects_empty_or_whitespace_only_text(value: str) -> None:
    assert_request_invalid(lambda: LLMTextContentPart(text=value))


def test_assistant_accepts_text_tool_calls_or_both() -> None:
    call = tool_call()

    assert message("assistant", "先查询资料").content == (text("先查询资料"),)
    assert LLMMessage(role="assistant", tool_calls=(call,)).tool_calls == (call,)
    assert LLMMessage(
        role="assistant",
        content=(text("我来查询"),),
        tool_calls=(call,),
    ).content == (text("我来查询"),)
    assert_request_invalid(lambda: LLMMessage(role="assistant"))


def test_tool_message_requires_one_text_part_and_tool_call_id() -> None:
    tool = LLMMessage(
        role="tool",
        content=(text('{"result":"ok"}'),),
        tool_call_id="call-1",
    )

    assert tool.tool_call_id == "call-1"
    assert_request_invalid(
        lambda: LLMMessage(role="tool", content=(text("result"),))
    )
    assert_request_invalid(
        lambda: LLMMessage(role="tool", tool_call_id="call-1")
    )


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_multiple_pure_text_parts_are_rejected(role: str) -> None:
    assert_request_invalid(
        lambda: LLMMessage(
            role=role,  # type: ignore[arg-type]
            content=(text("一"), text("二")),
        )
    )


def test_user_multimodal_shape_is_structurally_valid_but_local_paths_are_rejected() -> None:
    image = LLMImageURLContentPart(image_url="https://example.invalid/casting.png")
    user = LLMMessage(role="user", content=(text("分析图片"), image))

    assert user.content == (text("分析图片"), image)
    for invalid_url in ("file:///tmp/casting.png", "C:\\casting.png", "./casting.png"):
        assert_request_invalid(
            lambda invalid_url=invalid_url: LLMImageURLContentPart(image_url=invalid_url)
        )


def test_illegal_role_and_content_field_combinations_are_rejected() -> None:
    call = tool_call()

    assert_request_invalid(lambda: LLMMessage(role="developer", content=(text("x"),)))
    assert_request_invalid(
        lambda: LLMMessage(role="system", content=(text("x"),), tool_calls=(call,))
    )
    assert_request_invalid(
        lambda: LLMMessage(role="user", content=(text("x"),), tool_call_id="call-1")
    )
    assert_request_invalid(
        lambda: LLMMessage(
            role="assistant",
            content=(LLMImageURLContentPart(image_url="https://example.invalid/x.png"),),
        )
    )


def test_tool_call_ids_are_globally_unique_across_message_sequence() -> None:
    duplicate = tool_call("duplicate")

    assert_request_invalid(
        lambda: LLMGenerateRequest(
            messages=(
                LLMMessage(role="assistant", tool_calls=(duplicate,)),
                LLMMessage(role="assistant", tool_calls=(duplicate,)),
            )
        )
    )


def test_tool_message_must_reference_an_earlier_assistant_tool_call() -> None:
    call = tool_call("call-1")
    request = LLMGenerateRequest(
        messages=(
            LLMMessage(role="assistant", tool_calls=(call,)),
            LLMMessage(
                role="tool",
                content=(text("result"),),
                tool_call_id="call-1",
            ),
            message("user", "继续"),
        )
    )

    assert request.messages[1].tool_call_id == "call-1"


@pytest.mark.parametrize(
    "messages",
    [
        (
            LLMMessage(role="tool", content=(text("result"),), tool_call_id="missing"),
        ),
        (
            LLMMessage(role="tool", content=(text("result"),), tool_call_id="future"),
            LLMMessage(role="assistant", tool_calls=(tool_call("future"),)),
        ),
    ],
)
def test_tool_message_cannot_reference_missing_or_future_call(
    messages: tuple[LLMMessage, ...],
) -> None:
    assert_request_invalid(lambda: LLMGenerateRequest(messages=messages))


def test_tool_call_accepts_at_most_one_response() -> None:
    response = LLMMessage(
        role="tool",
        content=(text("result"),),
        tool_call_id="call-1",
    )

    assert_request_invalid(
        lambda: LLMGenerateRequest(
            messages=(
                LLMMessage(role="assistant", tool_calls=(tool_call(),)),
                response,
                response,
            )
        )
    )


def test_tool_call_may_remain_unanswered() -> None:
    request = LLMGenerateRequest(
        messages=(LLMMessage(role="assistant", tool_calls=(tool_call(),)),)
    )

    assert request.messages[0].tool_calls[0].id == "call-1"


def test_request_preserves_text_multi_turn_and_assistant_history_order() -> None:
    request = LLMGenerateRequest(
        messages=(
            message("system", "只回答铸造问题"),
            message("user", "什么是砂型？"),
            message("assistant", "砂型是用型砂制成的铸型。"),
            message("user", "它有哪些组成部分？"),
        )
    )

    assert [item.role for item in request.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert request.messages[2].content == (text("砂型是用型砂制成的铸型。"),)


@pytest.mark.parametrize("parameters", [None, [], "object", 1])
def test_function_tool_parameters_top_level_must_be_object(parameters: object) -> None:
    assert_request_invalid(
        lambda: LLMFunctionTool(name="lookup", parameters=parameters)  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("bad_value", [object(), math.nan, math.inf, -math.inf])
def test_function_tool_parameters_must_be_strict_json(bad_value: object) -> None:
    assert_request_invalid(
        lambda: LLMFunctionTool(
            name="lookup",
            parameters={"type": "object", "invalid": bad_value},
        )
    )


def test_tool_parameter_validation_detaches_serialization_exception_context() -> None:
    marker = "sensitive-tool-parameter"

    with pytest.raises(BusinessError) as exc_info:
        LLMFunctionTool(
            name="lookup",
            parameters={"type": "object", "invalid": RuntimeError(marker)},
        )

    error = exc_info.value
    assert error.code == LLM_REQUEST_INVALID
    assert error.__cause__ is None
    assert error.__context__ is None
    assert marker not in "".join(traceback.format_exception(error))
    assert marker not in repr(error.detail)


def test_malformed_image_url_is_request_invalid_without_parser_context() -> None:
    marker = "sensitive-image-host"

    with pytest.raises(BusinessError) as exc_info:
        LLMImageURLContentPart(image_url=f"https://[{marker}")

    error = exc_info.value
    assert error.code == LLM_REQUEST_INVALID
    assert error.detail == {"field": "image_url"}
    assert error.__cause__ is None
    assert error.__context__ is None
    assert marker not in "".join(traceback.format_exception(error))


def test_function_tool_parameters_are_defensively_deep_copied() -> None:
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
    }
    function_tool = LLMFunctionTool(name="lookup", parameters=parameters)

    parameters["properties"]["query"]["type"] = "number"
    first_read = function_tool.parameters
    first_read["properties"]["query"]["type"] = "boolean"

    assert function_tool.parameters["properties"]["query"]["type"] == "string"


def test_from_prompt_creates_one_user_message() -> None:
    request = LLMGenerateRequest.from_prompt("用户问题")

    assert request.messages == (message("user", "用户问题"),)


def test_from_prompt_with_system_prompt_preserves_system_then_user_order() -> None:
    request = LLMGenerateRequest.from_prompt(
        "用户问题",
        "系统提示",
        temperature=0.3,
        max_tokens=512,
        json_mode=True,
        think=False,
        timeout_seconds=10,
        stop=("END",),
    )

    assert request.messages == (
        message("system", "系统提示"),
        message("user", "用户问题"),
    )
    assert request.json_mode is True
    assert request.think is False


@pytest.mark.parametrize(
    ("legacy_name", "legacy_value"),
    [
        ("prompt", "旧业务用户问题"),
        ("system_prompt", "旧业务系统提示"),
        ("response_format", {"type": "json_object"}),
    ],
)
def test_request_constructor_rejects_removed_legacy_parameters(
    legacy_name: str,
    legacy_value: object,
) -> None:
    with pytest.raises(TypeError):
        LLMGenerateRequest(  # type: ignore[call-arg]
            **{legacy_name: legacy_value},
        )


def test_formal_request_fields_and_signature_exclude_legacy_inputs() -> None:
    field_names = {field.name for field in fields(LLMGenerateRequest)}
    parameter_names = set(inspect.signature(LLMGenerateRequest).parameters)

    assert "messages" in field_names
    for legacy_name in ("prompt", "system_prompt", "response_format"):
        assert legacy_name not in field_names
        assert legacy_name not in parameter_names


def test_result_requires_assistant_message_and_has_no_raw_field() -> None:
    result = LLMGenerateResult(
        message=message("assistant", "回答"),
        provider="local",
        model="qwen3:8b",
    )

    assert result.message.role == "assistant"
    assert "raw" not in {field.name for field in fields(LLMGenerateResult)}
    assert "text" not in {field.name for field in fields(LLMGenerateResult)}
    with pytest.raises(BusinessError) as exc_info:
        LLMGenerateResult(
            message=message("user", "不是 assistant"),
            provider="local",
            model="qwen3:8b",
        )
    assert exc_info.value.code == LLM_RESPONSE_INVALID


def test_result_text_returns_the_unique_assistant_text() -> None:
    result = LLMGenerateResult(
        message=message("assistant", "唯一回答"),
        provider="api",
        model="remote-model",
    )

    assert result.text == "唯一回答"


def test_result_text_rejects_assistant_message_without_text() -> None:
    result = LLMGenerateResult(
        message=LLMMessage(role="assistant", tool_calls=(tool_call(),)),
        provider="api",
        model="remote-model",
    )

    with pytest.raises(BusinessError) as exc_info:
        _ = result.text

    assert exc_info.value.code == LLM_RESPONSE_INVALID


def test_result_constructor_rejects_removed_text_parameter() -> None:
    with pytest.raises(TypeError):
        LLMGenerateResult(  # type: ignore[call-arg]
            text="旧文本结果",
            provider="fake",
            model="fake-model",
        )


def test_capabilities_reject_parallel_tools_without_tools_support() -> None:
    assert_request_invalid(
        lambda: LLMCapabilities(
            supports_json_mode=False,
            supports_think=False,
            supports_tools=False,
            supports_parallel_tool_calls=True,
            supports_image_input=False,
        )
    )
