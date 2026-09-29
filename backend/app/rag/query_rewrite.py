"""LLM-only question understanding with structural and ownership checks."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from time import perf_counter
from uuid import UUID

from app.core.config import Settings
from app.core.errors import BusinessError
from app.llm.messages import LLMMessage, LLMTextContentPart
from app.llm.provider import LLMGenerateRequest, LLMProvider
from app.rag.history_budget import HistoryContext, estimated_tokens
from app.rag.query_rewrite_prompt import PROMPT_FINGERPRINT, SYSTEM_PROMPT
from app.schemas.conversation_persistence import PersistenceMetrics, TokenUsage
from app.schemas.query_rewrite import RewriteArtifactDetails, RewriteResult


def clarification_text(result: RewriteResult) -> str:
    if result.decision != "clarify":
        raise ValueError("Not a clarification result")
    text = result.clarification_reason
    if result.clarification_options:
        text += "\n可补充说明：" + "；".join(result.clarification_options)
    return text


def parse_result(text: str, *, max_bytes: int) -> RewriteResult:
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("Rewrite output exceeds its byte budget")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def no_constant(_):
        raise ValueError("Non-finite JSON")

    parsed = json.loads(text, object_pairs_hook=unique_object, parse_constant=no_constant)
    return RewriteResult.model_validate_json(json.dumps(parsed, ensure_ascii=False))


def validate_result(result: RewriteResult, current_id: UUID, context: HistoryContext,
                    *, max_query_bytes: int) -> None:
    """No inspection of wording, materials, parameters or semantic decisions."""
    available = {m.id for m in context.messages} | {current_id}
    if not set(result.referenced_message_ids) <= available:
        raise ValueError("Reference not in this execution's input")
    if any(not set(ref.source_message_ids) <= available for ref in result.resolved_references):
        raise ValueError("Reference source not in this execution's input")
    if len((result.standalone_query or "").encode("utf-8")) > max_query_bytes:
        raise ValueError("Retrieval query exceeds its byte budget")
    if result.history_scope == "clarification" and context.pending is None:
        raise ValueError("Pending clarification identity was not supplied")


def _messages(question: str, current_id: UUID, context: HistoryContext) -> tuple[LLMMessage, ...]:
    result = [LLMMessage("system", (LLMTextContentPart(SYSTEM_PROMPT),))]
    result.extend(LLMMessage(m.role, (LLMTextContentPart(json.dumps(
        {"message_id": str(m.id), "text": m.content}, ensure_ascii=False)),)) for m in context.messages)
    pending = context.pending
    payload = {"message_id": str(current_id), "question": question,
               "pending_clarification": {"message_id": str(pending.message_id),
                   "question": pending.question, "options": list(pending.options)} if pending else None}
    result.append(LLMMessage("user", (LLMTextContentPart(json.dumps(payload, ensure_ascii=False)),)))
    return tuple(result)


def _prompt_cost(messages: tuple[LLMMessage, ...]) -> tuple[int, int]:
    text = json.dumps([{"role": m.role, "content": m.content[0].text} for m in messages],
                      ensure_ascii=False, separators=(",", ":"))
    return len(text.encode("utf-8")), estimated_tokens(text) + 32 * (len(messages) + 1)


def rewrite_input_fingerprint(question: str, current_id: UUID, context: HistoryContext, settings: Settings) -> str:
    data = {"messages": [{"role": m.role, "content": m.content[0].text}
                         for m in _messages(question, current_id, context)],
            "temperature": settings.conversation_rewrite_temperature,
            "max_tokens": settings.conversation_rewrite_max_tokens}
    return sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RewriteRun:
    details: RewriteArtifactDetails


class QueryRewriter:
    def __init__(self, provider: LLMProvider, settings: Settings):
        self.provider, self.settings = provider, settings

    def understand(self, question: str, current_id: UUID, context: HistoryContext) -> RewriteRun:
        settings = self.settings
        if not question.strip() or len(question) > 2000 or len(question.encode("utf-8")) > settings.conversation_question_max_bytes:
            raise BusinessError("QA_REWRITE_INPUT_BUDGET_EXCEEDED", "Question exceeds rewrite input limits.", status_code=422)
        if context.budget_exhausted:
            raise BusinessError("QA_CONTEXT_BUDGET_EXCEEDED", "Required conversation context does not fit.", status_code=422)
        used = context
        # Protect newest complete round and pending original. Budget is not semantics.
        protected = {context.messages[-1].turn_id} if context.messages else set()
        if context.pending:
            protected.add(context.pending.turn_id)
        while True:
            messages = _messages(question, current_id, used)
            size, tokens = _prompt_cost(messages)
            if size <= settings.conversation_rewrite_max_input_bytes and tokens <= settings.conversation_rewrite_max_estimated_tokens:
                break
            removable = next((m.turn_id for m in used.messages if m.turn_id not in protected), None)
            if removable is None:
                code = "QA_CONTEXT_BUDGET_EXCEEDED" if used.messages else "QA_REWRITE_INPUT_BUDGET_EXCEEDED"
                raise BusinessError(code, "Required rewrite input does not fit.", status_code=422)
            used = HistoryContext(tuple(m for m in used.messages if m.turn_id != removable), used.pending)

        started = perf_counter()
        try:
            generated = self.provider.generate(LLMGenerateRequest(
                messages=messages, temperature=settings.conversation_rewrite_temperature,
                max_tokens=settings.conversation_rewrite_max_tokens,
                json_mode=self.provider.capabilities.supports_json_mode,
                think=False if self.provider.capabilities.supports_think else None,
                timeout_seconds=settings.conversation_rewrite_timeout_seconds,
            ))
        except TimeoutError:
            raise BusinessError("LLM_TIMEOUT", "Question understanding timed out.", status_code=504) from None
        try:
            result = parse_result(generated.text, max_bytes=settings.conversation_rewrite_max_output_bytes)
            validate_result(result, current_id, used, max_query_bytes=settings.conversation_question_max_bytes)
        except (ValueError, TypeError, RecursionError):
            raise BusinessError("QA_REWRITE_OUTPUT_INVALID", "Question understanding returned invalid structured output.", status_code=502) from None
        metrics = PersistenceMetrics(provider=generated.provider, model=generated.model,
            latency_ms=int((perf_counter() - started) * 1000), usage=TokenUsage(
                input_tokens=generated.usage.prompt_tokens, output_tokens=generated.usage.completion_tokens,
                total_tokens=generated.usage.total_tokens) if generated.usage else None)
        pending_id = used.pending.turn_id if used.pending else None
        return RewriteRun(RewriteArtifactDetails(
            prompt_fingerprint=PROMPT_FINGERPRINT, result=result, history_message_ids=used.message_ids,
            input_fingerprint=rewrite_input_fingerprint(question, current_id, used, settings),
            input_pending_clarification_turn_id=pending_id,
            pending_clarification_turn_id=pending_id if result.decision != "standalone" and result.history_scope == "clarification" else None,
            input_bytes=size, input_estimated_tokens=tokens, metrics=metrics,
            validation_codes=["json_structure", "bounded_output", "owned_references"],
        ))
