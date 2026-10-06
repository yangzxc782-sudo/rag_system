import json
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.core.errors import BusinessError
from app.llm.api import APILLMProvider
from app.llm.local import LocalLLMProvider
from app.llm.messages import LLMFunctionCall, LLMMessage, LLMTextContentPart, LLMToolCall, parse_tool_arguments
from app.llm.provider import LLMGenerateRequest
from app.rag.casting_nodes import route_result
from app.rag.casting_prompt import TOOL_NAME, routing_request, tool_result_request
from app.rag.conversation_state import StateContractV3, validate_state
from app.schemas.casting_graph import CastingToolInput


def context(fid=None):
    return dict(question="请计算浇冒系统方案", effective_input_file_id=fid, input_source="current_message" if fid else "none", recent_runs=[])


def call_message(fid, *, name=TOOL_NAME, args=None):
    return LLMMessage("assistant", tool_calls=(LLMToolCall("call_1", LLMFunctionCall(name, args or json.dumps({"input_file_id": fid}))),))


def completion(fid, **changes):
    message = NS(role="assistant", content=None, tool_calls=[NS(id="call_1", type="function",
        function=NS(name=TOOL_NAME, arguments=json.dumps({"input_file_id": fid})))])
    for key, value in changes.items():
        setattr(message, key, value)
    return NS(choices=[NS(message=message, finish_reason="tool_calls")], usage=None, id="safe_request")


class Client:
    def __init__(self, response):
        self.response, self.calls = response, []
        self.chat = NS(completions=NS(create=self.create))
    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def settings(**changes):
    return Settings(_env_file=None, llm_provider="api", llm_remote_base_url="https://example.invalid/v1",
        llm_remote_api_key="synthetic-test-key", llm_remote_model="gpt-4o-mini", **changes)


def test_native_call_empty_text_and_tool_response_wire_protocol():
    fid = str(uuid4())
    client = Client(completion(fid))
    provider = APILLMProvider(settings(llm_remote_supports_tools=True), client=client)
    result = provider.generate(routing_request(context(fid)))
    assert result.message.content == () and result.message.tool_calls[0].function.name == TOOL_NAME
    payload = client.calls[0]
    assert payload["parallel_tool_calls"] is False and payload["tools"][0]["function"]["parameters"]["additionalProperties"] is False
    assert "extra_body" not in payload
    client.response = completion(fid, tool_calls=None, content="已收到工具结果。")
    provider.generate(tool_result_request("解释结果", result.message, {"status": "success", "candidate_count": 4}))
    payload = client.calls[-1]
    assert "tools" not in payload and "parallel_tool_calls" not in payload
    assert payload["messages"][-2]["content"] is None
    assert payload["messages"][-2]["tool_calls"][0]["id"] == payload["messages"][-1]["tool_call_id"] == "call_1"
    assert json.loads(payload["messages"][-1]["content"])["candidate_count"] == 4


@pytest.mark.parametrize("enabled", [False, True])
def test_tool_capability_is_explicit_and_text_requests_unchanged(enabled):
    fid = str(uuid4())
    client = Client(completion(fid, content="普通回答", tool_calls=None))
    provider = APILLMProvider(settings(llm_remote_supports_tools=enabled), client=client)
    provider.generate(LLMGenerateRequest.from_prompt("普通问题"))
    assert set(client.calls[-1]) == {"model", "messages", "temperature", "max_tokens"}
    if not enabled:
        with pytest.raises(BusinessError) as caught:
            provider.generate(routing_request(context(fid)))
        assert caught.value.code == "LLM_PARAMETER_UNSUPPORTED" and len(client.calls) == 1
    local = LocalLLMProvider(Settings(_env_file=None, llm_local_supports_tools=enabled), client=client)
    assert local.capabilities.supports_tools is enabled and not local.capabilities.supports_parallel_tool_calls


@pytest.mark.parametrize("case", ["unrequested", "multiple", "unknown", "duplicate_json", "array", "truncated", "bad_id"])
def test_transport_rejects_bad_native_protocol(case):
    fid = str(uuid4())
    value = completion(fid)
    call = value.choices[0].message.tool_calls[0]
    if case == "multiple": value.choices[0].message.tool_calls *= 2
    if case == "unknown": call.function.name = "arbitrary_shell"
    if case == "duplicate_json": call.function.arguments = '{"input_file_id":"a","input_file_id":"b"}'
    if case == "array": call.function.arguments = "[]"
    if case == "truncated": value.choices[0].finish_reason = "length"
    if case == "bad_id": call.id = "\nprivate"
    provider = APILLMProvider(settings(llm_remote_supports_tools=True), client=Client(value))
    request = LLMGenerateRequest.from_prompt("hello") if case == "unrequested" else routing_request(context(fid))
    with pytest.raises(BusinessError) as caught:
        provider.generate(request)
    assert caught.value.code == "LLM_RESPONSE_INVALID"


@pytest.mark.parametrize("raw", ['{"x":NaN}', '{"x":1e999}', '{"x":"\\ud800"}', '{"x":1,"x":2}', "[]", "{" + '"x":' + "[" * 40 + "0" + "]" * 40 + "}"])
def test_strict_tool_json(raw):
    with pytest.raises((ValueError, RecursionError)):
        parse_tool_arguments(raw)


@pytest.mark.parametrize("case", ["wrong_file", "no_file", "extra_args", "path", "unknown", "many", "json_calculate"])
def test_router_never_executes_invalid_tools(case):
    fid = str(uuid4())
    ctx, msg = context(fid), call_message(fid)
    if case == "wrong_file": msg = call_message(str(uuid4()))
    if case == "no_file": ctx = context()
    if case == "extra_args": msg = call_message(fid, args=json.dumps({"input_file_id": fid, "mass": 1}))
    if case == "path": msg = call_message("D:/private/input.json")
    if case == "unknown": msg = call_message(fid, name="shell")
    if case == "many": msg = LLMMessage("assistant", tool_calls=msg.tool_calls * 2)
    if case == "json_calculate": msg = LLMMessage("assistant", (LLMTextContentPart('{"route":"calculate"}'),))
    with pytest.raises(BusinessError) as caught:
        route_result(msg, ctx)
    assert caught.value.code == "CASTING_TOOL_CALL_INVALID"


def test_valid_route_and_history_reference():
    fid, rid = str(uuid4()), str(uuid4())
    assert route_result(call_message(fid), context(fid)).route == "calculate"
    ctx = context(fid)
    ctx["recent_runs"] = [{"run_id": rid, "turn_id": str(uuid4())}]
    msg = LLMMessage("assistant", (LLMTextContentPart(json.dumps({"route": "explain_existing", "source_run_id": rid})),))
    assert route_result(msg, ctx).source_run_id == rid
    with pytest.raises(BusinessError):
        route_result(msg, context(fid))


@pytest.mark.parametrize("question", ["把质量改为 500 kg，再重新计算方案。", "改成球铁再算", "质量 500 kg，重新设计。", "Change mass to 500 kg and calculate."])
def test_history_input_edits_cannot_authorize_old_file_calculation(question):
    fid = str(uuid4())
    ctx = {**context(fid), "question": question, "input_source": "session_history"}
    assert route_result(call_message(fid), ctx).route == "input_required"
    assert json.loads(routing_request(ctx).messages[-1].content[0].text)["requires_updated_input"] is True
    # A current selection is frozen server-side and may be computed as uploaded.
    ctx["input_source"] = "current_message"
    assert route_result(call_message(fid), ctx).route == "calculate"


def test_history_recalculation_and_knowledge_are_not_parameter_changes():
    fid = str(uuid4())
    ctx = {**context(fid), "question": "用之前的输入重新计算一次。", "input_source": "session_history"}
    assert route_result(call_message(fid), ctx).route == "calculate"
    ctx["question"] = "调整冒口有哪些原则？"
    msg = LLMMessage("assistant", (LLMTextContentPart('{"route":"rag"}'),))
    assert route_result(msg, ctx).route == "rag"


@pytest.mark.parametrize("provider_name, field", [("local", "llm_local_supports_tools"), ("api", "llm_remote_supports_tools")])
def test_capability_config_rejects_truthy_non_boolean(provider_name, field):
    from app.llm.configuration import validate_active_llm_configuration
    cfg = settings().model_copy(update={"llm_provider": provider_name, field: "false"})
    with pytest.raises(BusinessError) as caught:
        validate_active_llm_configuration(cfg)
    assert caught.value.code == "LLM_CONFIG_INVALID"


def test_v3_state_rejects_message_or_engine_payload():
    state = StateContractV3(thread_id=str(uuid4()), turn_id=str(uuid4()), request_id=str(uuid4()), attempt_no=1,
        input_fingerprint="a" * 64, current_message_id=str(uuid4()), evidence_generation=1).model_dump()
    assert validate_state(state)["graph_version"] == "casting_v1_v3"
    for key in ("messages", "recommendation", "input_json", "tool_calls"):
        with pytest.raises(ValueError):
            validate_state({**state, key: []})
