"""M3 adapters over the existing RAG implementation; no second model/search stack."""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

from app.core.config import Settings
from app.rag.context_builder import RagContext
from app.rag.graph_context_builder import GraphContext, format_graph_context_for_prompt
from app.rag.history_budget import HistoryContext, estimated_tokens
from app.rag.conversation_prompt import input_budget, messages_for_answer, prompt_cost
from app.services import rag
from app.services.conversation_repository import ConversationError


@dataclass(frozen=True)
class EvidencePlan:
    context: RagContext
    graph: GraphContext
    history: HistoryContext
    graph_triggered: bool


def validate_graph_sources(graph: GraphContext, context: RagContext) -> GraphContext:
    """Drop a whole graph unit if any trigger is outside the final text context."""
    _, origins = rag.extract_graph_refs(context)
    allowed = {(p.anchor_id, p.document_id, p.chunk_id, p.citation_id) for p in origins}
    kept = tuple(item for item in graph.evidence if item.provenance and all(
        p.anchor_id == item.ref.anchor_id and (p.anchor_id, p.document_id, p.chunk_id, p.citation_id) in allowed
        for p in item.provenance))
    return replace(graph, evidence=kept, status="ok" if kept else ("empty" if graph.enabled else "disabled"),
                   was_truncated=graph.was_truncated or len(kept) != len(graph.evidence))


class ChatEvidenceService:
    def __init__(self, session_factory, settings: Settings, *, graph_retrieval=None):
        self.session_factory, self.settings = session_factory, settings
        self.graph_retrieval = graph_retrieval

    def retrieve(self, query, limit, document_id):
        # Hybrid owns a short deletion-filter session. No surrounding business
        # transaction spans Embedding, OpenSearch, or BGE.
        return rag.retrieve_and_rerank(None, query, limit, document_id, self.settings,
                                       deletion_filter_session_factory=self.session_factory)

    def build(self, question, query, history: HistoryContext, search_result, *, preserve_order: bool) -> EvidencePlan:
        settings = self.settings
        limit = input_budget(settings)
        empty_graph = GraphContext(enabled=settings.graph_retrieval_enabled,
                                   status="empty" if settings.graph_retrieval_enabled else "disabled")
        empty = RagContext(query, "no_context", [], 0, 0)
        def cost(context, graph=empty_graph):
            return prompt_cost(messages_for_answer(question, query, history, context, graph, settings))
        # Preserve necessary clarification history; otherwise evict oldest whole
        # rounds when fixed prompt overhead + history alone exceeds the ceiling.
        while cost(empty) > limit:
            tid = next((m.turn_id for m in history.messages if not history.pending or m.turn_id != history.pending.turn_id), None)
            if tid is None:
                raise ConversationError("QA_PROMPT_BUDGET_EXCEEDED", "Question and required history exceed answer budget.", status_code=422)
            history = HistoryContext(tuple(m for m in history.messages if m.turn_id != tid), history.pending)
        reserve = min(settings.conversation_answer_graph_tokens, max(0, limit - cost(empty))) if settings.graph_retrieval_enabled else 0
        def fit_text(ceiling):
            low, high, best = 0, settings.rag_context_max_chars, empty
            while low <= high:
                mid = (low + high) // 2
                candidate = rag.build_context(query, search_result, SimpleNamespace(rag_context_max_chars=mid),
                                              preserve_order=preserve_order)
                if cost(candidate) <= ceiling:
                    best, low = candidate, mid + 1
                else:
                    high = mid - 1
            return best
        context = fit_text(limit - reserve)
        if not context.chunks and reserve:
            context, reserve = fit_text(limit), 0
        # Neo4j is called only now, from the FINAL retained text chunks.
        graph = (rag.build_graph_context_for_rag(context, settings, self.graph_retrieval)
                 if context.chunks and reserve else empty_graph)
        triggered = bool(context.chunks and settings.graph_retrieval_enabled and reserve and rag.extract_graph_refs(context)[0])
        try:
            graph = validate_graph_sources(graph, context)
            while graph.evidence and (estimated_tokens(format_graph_context_for_prompt(graph)) > reserve or cost(context, graph) > limit):
                graph = replace(graph, evidence=graph.evidence[:-1], was_truncated=True)
            graph = replace(graph, status="ok" if graph.evidence else ("empty" if graph.enabled else "disabled"))
            graph = replace(graph, total_chars=len(format_graph_context_for_prompt(graph)))
        except Exception:
            # Match the established optional graph/formatter fail-open contract.
            graph = empty_graph
        if cost(context, graph) > limit:
            raise ConversationError("QA_PROMPT_BUDGET_EXCEEDED", "Final answer serialization exceeds budget.", status_code=422)
        return EvidencePlan(context, graph, history, triggered)
