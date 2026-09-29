"""LLM contract tests; synthetic responses do not claim model semantic quality."""
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import BusinessError
from app.llm.provider import LLMCapabilities
from app.rag.conversation_state import StateContract, validate_state
from app.rag.history_budget import HistoryContext, HistoryMessage, PendingClarification, estimated_tokens, trim_history
from app.rag.query_rewrite import QueryRewriter, clarification_text, parse_result, validate_result
from app.rag.query_rewrite_prompt import SYSTEM_PROMPT
from phase13_m2_support import FakeProvider, settings_for


def history(question="冒口有什么作用？", answer="冒口用于补缩。", *, count=1):
    messages = []
    for i in range(count):
        tid = uuid4()
        messages += [HistoryMessage(uuid4(), tid, "user", question, i * 2 + 1),
                     HistoryMessage(uuid4(), tid, "assistant", answer, i * 2 + 2)]
    return HistoryContext(tuple(messages))


def pending_context():
    context = history("它的尺寸怎么确定？", "请说明对象。")
    user = context.messages[0]
    return HistoryContext(context.messages, PendingClarification(user.turn_id, user.content, user.id, ("冒口", "冷铁")))


def result(decision="standalone", query="独立检索问题", **fields):
    return dict(decision=decision, standalone_query=None if decision == "clarify" else query,
        history_scope="none", referenced_message_ids=[], resolved_references=[],
        clarification_reason="请说明讨论对象。" if decision == "clarify" else None,
        clarification_options=[], **fields) if not fields else {
            **result(decision, query), **fields}


@pytest.mark.parametrize("question,context,response", [
    ("WCB的力学性能如何？", HistoryContext(), result(query="WCB力学性能")),
    ("完整的新话题？", history(), result(query="新话题的独立表述")),
    ("它的成分含有什么？", history("WCB的力学性能如何？"), result("rewritten", "WCB的化学成分包括哪些元素？", history_scope="recent")),
    ("有哪些限制条件？", history(), result("rewritten", "冒口设计的适用条件与限制是什么？", history_scope="recent")),
    ("它的尺寸呢？", HistoryContext(), result("clarify")),
    ("冒口", pending_context(), result("rewritten", "如何确定冒口的设计尺寸？", history_scope="clarification")),
    ("新话题？", pending_context(), result(query="新话题的完整问题")),
])
def test_every_valid_input_is_decided_by_one_model_call(question, context, response):
    provider = FakeProvider(response)
    details = QueryRewriter(provider, settings_for()).understand(question, uuid4(), context).details
    assert details.result.model_dump(mode="json") == response
    assert len(provider.calls) == 1
    assert details.history_message_ids == context.message_ids
    assert details.metrics.usage.total_tokens == 130
    assert details.strategy == "llm_v2"
    assert details.validation_codes == ["json_structure", "bounded_output", "owned_references"]
    request = provider.calls[0]
    assert request.temperature == 0 and request.timeout_seconds == 15
    assert request.max_tokens == 768 and request.json_mode and request.think is False


@pytest.mark.parametrize("decision", ["standalone", "rewritten", "clarify"])
def test_program_does_not_override_model_decision_based_on_wording(decision):
    response = result(decision, "不含机械替换关系的自然语言问题")
    provider = FakeProvider(response)
    actual = QueryRewriter(provider, settings_for()).understand("它与WCB的成分有什么关系？", uuid4(), history()).details.result
    assert actual.decision == decision and len(provider.calls) == 1


def test_pending_input_and_selected_relationship_are_distinct():
    context = pending_context()
    provider = FakeProvider(result("clarify", history_scope="none"))
    details = QueryRewriter(provider, settings_for()).understand("另一事项需要讨论", uuid4(), context).details
    payload = json.loads(provider.calls[0].messages[-1].content[0].text)
    assert payload["pending_clarification"] == {
        "question": context.pending.question, "message_id": str(context.pending.message_id),
        "options": ["冒口", "冷铁"]}
    assert details.input_pending_clarification_turn_id == context.pending.turn_id
    assert details.pending_clarification_turn_id is None
    assert "thread_id" not in payload and "attempt_no" not in payload


@pytest.mark.parametrize("response,code", [
    (TimeoutError("secret upstream body"), "LLM_TIMEOUT"),
    (BusinessError("LLM_UNAVAILABLE", "safe", status_code=503), "LLM_UNAVAILABLE"),
    ("not json", "QA_REWRITE_OUTPUT_INVALID"),
    ("[]", "QA_REWRITE_OUTPUT_INVALID"),
    ('{"decision":"rewritten","standalone_query":42}', "QA_REWRITE_OUTPUT_INVALID"),
    ({**result(), "thread_id": str(uuid4())}, "QA_REWRITE_OUTPUT_INVALID"),
    (result(query=" "), "QA_REWRITE_OUTPUT_INVALID"),
    (result("clarify", clarification_reason=" "), "QA_REWRITE_OUTPUT_INVALID"),
    (result(query="a" * 2001), "QA_REWRITE_OUTPUT_INVALID"),
])
def test_technical_failure_never_becomes_clarification(response, code):
    provider = FakeProvider(response)
    with pytest.raises(BusinessError) as caught:
        QueryRewriter(provider, settings_for()).understand("完整独立问题？", uuid4(), HistoryContext())
    assert caught.value.code == code and "secret" not in str(caught.value)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("field", ["referenced_message_ids", "resolved_references"])
def test_foreign_reference_is_a_technical_output_error(field):
    response = result("rewritten", history_scope="recent")
    response[field] = ([str(uuid4())] if field == "referenced_message_ids" else [
        {"surface": "它", "referent": "新表述", "source_message_ids": [str(uuid4())]}])
    with pytest.raises(BusinessError) as caught:
        QueryRewriter(FakeProvider(response), settings_for()).understand("它？", uuid4(), history())
    assert caught.value.code == "QA_REWRITE_OUTPUT_INVALID"


def test_explanatory_reference_need_not_match_literal_source_or_query():
    context = history(answer="第二种是经验法。")
    response = result("rewritten", "经验设计方法的适用边界是什么？", history_scope="recent",
        resolved_references=[{"surface": "第二个", "referent": "经验设计方法",
                              "source_message_ids": [str(context.messages[1].id)]}])
    actual = QueryRewriter(FakeProvider(response), settings_for()).understand("第二个有什么限制？", uuid4(), context)
    assert actual.details.result.standalone_query == response["standalone_query"]


@pytest.mark.parametrize("output", [
    '{"decision":"standalone","decision":"clarify"}', "```json\n{}\n```", '{"x":NaN}',
    "{} trailing", "{}", "null",
])
def test_parser_rejects_non_strict_json(output):
    with pytest.raises(ValueError):
        parse_result(output, max_bytes=8192)


def test_output_budget_and_empty_or_duplicate_fields():
    with pytest.raises(ValueError):
        parse_result(json.dumps(result()), max_bytes=10)
    ident = uuid4()
    for change in [{"referenced_message_ids": [str(ident)] * 2},
                   {"clarification_options": [" "]},
                   {"clarification_options": ["x"] * 7}]:
        with pytest.raises(ValueError):
            parse_result(json.dumps({**result("clarify"), **change}), max_bytes=8192)


def test_budget_counts_utf8_and_removes_whole_old_rounds():
    assert estimated_tokens("冒口") == 6
    context = history(count=20)
    selected = trim_history(context.messages, settings_for())
    assert len(selected) == 12 and selected[0].sequence_no == 29
    tiny = trim_history(context.messages, settings_for(conversation_history_max_bytes=500))
    assert len(tiny) < 12 and len(tiny) % 2 == 0 and tiny[-1] == context.messages[-1]
    pinned = context.messages[0].turn_id
    assert trim_history(context.messages, settings_for(), pinned_turn_id=pinned)[0].turn_id == pinned


@pytest.mark.parametrize("context,changes,code", [
    (HistoryContext(budget_exhausted=True), {}, "QA_CONTEXT_BUDGET_EXCEEDED"),
    (HistoryContext(), {"conversation_rewrite_max_input_bytes": 1024}, "QA_REWRITE_INPUT_BUDGET_EXCEEDED"),
    (history(), {"conversation_rewrite_max_input_bytes": 1024}, "QA_CONTEXT_BUDGET_EXCEEDED"),
])
def test_budget_failure_is_not_a_semantic_decision(context, changes, code):
    provider = FakeProvider()
    with pytest.raises(BusinessError) as caught:
        QueryRewriter(provider, settings_for(**changes)).understand("完整的问题？", uuid4(), context)
    assert caught.value.code == code and not provider.calls


def test_complete_prompt_budget_removes_old_rounds_but_keeps_newest_and_pending():
    context = history(answer="历史正文" * 60, count=6)
    root = context.messages[0]
    context = HistoryContext(context.messages, PendingClarification(root.turn_id, root.content, root.id, ()))
    provider = FakeProvider(result("rewritten", history_scope="clarification"))
    details = QueryRewriter(provider, settings_for(conversation_rewrite_max_estimated_tokens=6000)).understand("补充对象", uuid4(), context).details
    assert root.id in details.history_message_ids and context.messages[-1].id in details.history_message_ids
    assert len(details.history_message_ids) < 12 and len(details.history_message_ids) % 2 == 0


def test_text_json_provider_uses_only_supported_capabilities():
    provider = FakeProvider(result())
    provider.capabilities = LLMCapabilities(False, False, False, False, False)
    QueryRewriter(provider, settings_for()).understand("问题", uuid4(), HistoryContext())
    assert provider.calls[0].json_mode is False and provider.calls[0].think is None


def test_clarification_displays_model_reason_and_options_without_source_phrase_matching():
    response = result("clarify", clarification_reason="希望比较哪类性能？", clarification_options=["高温性能", "常温性能"])
    actual = QueryRewriter(FakeProvider(response), settings_for()).understand("怎么比？", uuid4(), history()).details.result
    assert clarification_text(actual) == "希望比较哪类性能？\n可补充说明：高温性能；常温性能"


def test_prompt_owns_technical_condition_policy():
    assert "不得编造材料牌号" in SYSTEM_PROMPT
    assert "不能自动成为新增的用户工艺要求" in SYSTEM_PROMPT
    assert "不要求逐字摘录或机械替换" in SYSTEM_PROMPT


def test_state_is_closed_reference_only_and_bounded():
    state = StateContract(thread_id=str(uuid4()), turn_id=str(uuid4()), request_id=str(uuid4()),
        attempt_no=1, input_fingerprint="a" * 64, current_message_id=str(uuid4())).model_dump()
    assert len(json.dumps(validate_state(state)).encode()) < 2048
    for key in ["messages", "prompt", "evidence", "session", "model"]:
        with pytest.raises(ValidationError):
            validate_state({**state, key: "forbidden"})
    with pytest.raises(ValidationError):
        validate_state({**state, "history_message_ids": [str(uuid4()) for _ in range(13)]})
