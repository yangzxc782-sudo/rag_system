from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
import logging
from typing import Any
from uuid import UUID, uuid4

import app.services.hybrid_search as hybrid_search_service
import app.services.reranking as reranking_service
from app.core.config import get_settings
from app.core.errors import (
    LLM_GENERATION_FAILED,
    RAG_ANSWER_FAILED,
    RAG_CONFIG_INVALID,
    RAG_QUERY_EMPTY,
    BusinessError,
)
from app.llm.configuration import resolve_active_llm_metadata
from app.llm.provider import (
    LLMGenerateRequest,
    LLMGenerateResult,
    LLMProvider,
    get_llm_provider,
)
from app.rag.citations import RagCitation
from app.rag.citations import build_citations as build_context_citations
from app.rag.context_builder import RagContext, RagContextStatus, build_rag_context, format_context_for_prompt
from app.graph.models import GraphTriggerProvenance, KGRef
from app.rag.graph_context_builder import GraphContext, build_graph_context
from app.rag.prompt import build_rag_prompt, build_user_prompt
from app.services.graph_retrieval import GraphRetrievalService
from app.services.hybrid_search import HybridSearchResult
from app.retrieval.reranker import (
    FAILURE_REASONS, RerankCandidate, RerankError, RerankerConfig, RerankRequest,
    validate_request as validate_rerank_request,
    validate_result as validate_rerank_result,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RagPrompt:
    system_prompt: str
    user_prompt: str
    graph_context: GraphContext | None = None


@dataclass(frozen=True)
class RagAnswerResult:
    question: str
    answer: str
    context_status: RagContextStatus
    citations: list[RagCitation]
    retrieval: HybridSearchResult
    llm_provider: str | None
    llm_model: str | None
    graph_context: GraphContext | None = None
    graph_triggered: bool = False


def retrieve_chunks(
    db: Any,
    question: str,
    limit: int,
    document_id: UUID | None,
    settings: Any | None = None,
) -> HybridSearchResult:
    settings = settings or get_settings()
    try:
        return hybrid_search_service.hybrid_search_chunks(
            db,
            query=question,
            limit=limit,
            document_id=document_id,
            settings=settings,
        )
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            RAG_ANSWER_FAILED,
            "RAG retrieval orchestration failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc


@dataclass(frozen=True)
class _RerankPlan:
    hybrid_limit: int
    candidate_limit: int | None = None
    fallback_reason: str | None = None


@dataclass(frozen=True)
class RagRerankOutcome:
    """Internal selection only. Public retrieval fields retain their RRF meanings."""

    search_result: HybridSearchResult
    applied: bool
    fallback_reason: str | None


def _plan_rerank(limit: int, settings: Any) -> _RerankPlan:
    if not bool(getattr(settings, "reranker_enabled", False)):
        return _RerankPlan(limit, fallback_reason="disabled")
    try:
        config = RerankerConfig.from_settings(settings)
        # Capacity must be reachable with the unchanged Hybrid contract. This is
        # validation only, with no model/Embedding/client construction or search.
        hybrid_search_service._validate_hybrid_config(settings, config.candidate_limit)
    except (RerankError, BusinessError, AttributeError, TypeError, ValueError):
        return _RerankPlan(limit, fallback_reason="configuration_invalid")
    if limit > config.candidate_limit:
        return _RerankPlan(limit, config.candidate_limit, "public_limit_exceeds_reranker_capacity")
    return _RerankPlan(config.candidate_limit, config.candidate_limit)


def optional_rerank_chunks(
    question: str, search_result: HybridSearchResult, settings: Any, *,
    limit: int | None = None, plan: _RerankPlan | None = None,
) -> RagRerankOutcome:
    final_limit = search_result.limit if limit is None else limit
    plan = plan or _plan_rerank(final_limit, settings)
    original_items = tuple(search_result.items)
    selected = original_items[:final_limit]
    applied = False
    reason = plan.fallback_reason

    if reason is None and not original_items:
        reason = "no_candidates"
    if reason is None and (plan.candidate_limit is None or len(original_items) > plan.candidate_limit):
        # An out-of-contract upstream result must never expand validated GPU capacity.
        reason = "invalid_output"
    if reason is None:
        try:
            request = RerankRequest(
                request_id=uuid4().hex, query=question,
                candidates=tuple(RerankCandidate(item.chunk_id, rank, item.content)
                                 for rank, item in enumerate(original_items, start=1)),
            )
            validate_rerank_request(request)
            result = reranking_service.get_reranking_service(settings).rerank(request)
            if result.failure_reason is not None:
                reason = result.failure_reason if result.failure_reason in FAILURE_REASONS else "invalid_output"
            else:
                # Do not apply even one promoted item until all identities/scores validate.
                validate_rerank_result(request, result)
                by_id = {item.chunk_id: item for item in original_items}
                selected = tuple(by_id[score.chunk_id] for score in result.scores)[:final_limit]
                applied = True
        except RerankError as exc:
            reason = exc.reason
        except Exception:
            reason = "inference_exception"

    if not applied:
        selected = original_items[:final_limit]
    final_result = HybridSearchResult(
        query=search_result.query, limit=final_limit, total=len(selected), items=list(selected),
    )
    logger.info(
        "RAG reranker applied=%s fallback_reason=%s K=%d C=%s candidate_count=%d",
        applied, reason, final_limit, plan.candidate_limit, len(original_items),
    )
    return RagRerankOutcome(final_result, applied, reason)


def build_context(
    question: str, search_result: HybridSearchResult, settings: Any, *, preserve_order: bool = False,
) -> RagContext:
    try:
        return build_rag_context(question, search_result, settings, preserve_order=preserve_order)
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            RAG_ANSWER_FAILED,
            "RAG context construction failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc


def build_prompt(question: str, context: RagContext, settings: Any,
                 graph_context: GraphContext | None = None) -> RagPrompt:
    try:
        system_prompt, user_prompt = build_rag_prompt(question, context, settings, graph_context=graph_context)
        # M6's formatter can fall back to the exact text prompt. Observe that
        # outcome without changing prompt semantics or serializing graph again.
        # Comparing the baseline avoids mistaking a heading in source text for
        # a graph section. Unused evidence must never be exposed as used evidence.
        if graph_context is not None and graph_context.evidence:
            text_prompt = build_user_prompt(question, format_context_for_prompt(context))
            if user_prompt == text_prompt:
                graph_context = replace(graph_context, status="empty", evidence=(), total_chars=0)
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            RAG_ANSWER_FAILED,
            "RAG prompt construction failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc
    return RagPrompt(system_prompt=system_prompt, user_prompt=user_prompt, graph_context=graph_context)


def generate_answer(prompt: RagPrompt, llm_provider: LLMProvider, settings: Any) -> LLMGenerateResult:
    try:
        result = llm_provider.generate(
            LLMGenerateRequest.from_prompt(
                prompt.user_prompt,
                prompt.system_prompt,
                temperature=getattr(settings, "llm_temperature", None),
                max_tokens=getattr(settings, "llm_max_tokens", None),
            )
        )
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            RAG_ANSWER_FAILED,
            "RAG answer generation failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc

    if not str(getattr(result, "text", "") or "").strip():
        raise BusinessError(
            LLM_GENERATION_FAILED,
            "LLM returned an empty answer.",
            status_code=500,
        )
    return result


def build_citations(context: RagContext) -> list[RagCitation]:
    return build_context_citations(context)


def extract_graph_refs(context: RagContext) -> tuple[list[KGRef], tuple[GraphTriggerProvenance, ...]]:
    """Read only final budgeted text chunks. Preserve duplicates for M5 conflict checks."""
    refs: list[KGRef] = []
    origins: list[GraphTriggerProvenance] = []
    if context.context_status != "ok":
        return refs, ()
    fields = {"anchor_id", "graph_id", "anchor_type", "table_ref"}
    for chunk in context.chunks:
        metadata = chunk.source_metadata
        if not isinstance(metadata, dict) or not isinstance(metadata.get("kg_refs"), list):
            continue
        for value in metadata["kg_refs"]:
            if not isinstance(value, dict) or not fields <= value.keys():
                continue
            if any(not isinstance(value[key], str) or not value[key].strip()
                   for key in ("anchor_id", "graph_id", "anchor_type")):
                continue
            if value["table_ref"] is not None and not isinstance(value["table_ref"], str):
                continue
            ref = KGRef(**{key: value[key] for key in fields})
            refs.append(ref)
            source_range = metadata.get("source_range")
            origins.append(GraphTriggerProvenance(
                anchor_id=ref.anchor_id, document_id=chunk.document_id, chunk_id=chunk.chunk_id,
                citation_id=chunk.citation_id,
                source_range=deepcopy(source_range) if isinstance(source_range, dict) else None,
            ))
    return refs, tuple(origins)


def build_graph_context_for_rag(context: RagContext, settings: Any,
                                graph_retrieval: GraphRetrievalService | None) -> GraphContext:
    enabled = bool(getattr(settings, "graph_retrieval_enabled", False))
    max_chars = int(getattr(settings, "rag_graph_context_max_chars", 6000))
    empty = GraphContext(enabled=enabled, status="empty" if enabled else "disabled", max_chars=max_chars)
    if not enabled or context.context_status != "ok" or not context.chunks or graph_retrieval is None:
        return empty
    try:
        refs, provenance = extract_graph_refs(context)
        if not refs:
            return empty
        result = graph_retrieval.retrieve(refs, provenance=provenance)
        graph_context = build_graph_context(result, max_chars=max_chars)
        logger.info("RAG graph evidence: anchors=%d evidence=%d unavailable=%d timeout=%d truncated=%s",
                    len({ref.anchor_id for ref in refs}), len(graph_context.evidence),
                    sum(item.status == "unavailable" for item in result.items),
                    sum(item.status in {"timeout", "budget_exhausted"} for item in result.items),
                    graph_context.was_truncated)
        return graph_context
    except Exception:
        logger.warning("RAG graph evidence unavailable; continuing with text context.")
        return empty


def answer_question(
    db: Any,
    question: str,
    limit: int | None = None,
    document_id: UUID | None = None,
    settings: Any | None = None,
    llm_provider: LLMProvider | None = None,
    graph_retrieval: GraphRetrievalService | None = None,
) -> RagAnswerResult:
    settings = settings or get_settings()
    normalized_question = _normalize_question(question)
    normalized_limit = _normalize_limit(limit, settings)
    rerank_plan = _plan_rerank(normalized_limit, settings)

    search_result = retrieve_chunks(
        db,
        normalized_question,
        rerank_plan.hybrid_limit,
        document_id,
        settings=settings,
    )
    reranked = optional_rerank_chunks(
        normalized_question, search_result, settings, limit=normalized_limit, plan=rerank_plan,
    )
    search_result = reranked.search_result
    context = build_context(normalized_question, search_result, settings, preserve_order=reranked.applied)

    if context.context_status == "no_context":
        active_llm = resolve_active_llm_metadata(settings)
        return RagAnswerResult(
            question=normalized_question,
            answer=str(getattr(settings, "rag_no_context_message", "") or ""),
            context_status="no_context",
            citations=[],
            retrieval=search_result,
            llm_provider=active_llm.provider,
            llm_model=active_llm.model or None,
            graph_context=GraphContext() if bool(getattr(settings, "graph_retrieval_enabled", False)) else None,
        )

    graph_context = build_graph_context_for_rag(context, settings, graph_retrieval)
    prompt = build_prompt(normalized_question, context, settings, graph_context=graph_context)
    provider = llm_provider or get_llm_provider()
    llm_result = generate_answer(prompt, provider, settings)

    return RagAnswerResult(
        question=normalized_question,
        answer=llm_result.text.strip(),
        context_status="ok",
        citations=build_citations(context),
        retrieval=search_result,
        llm_provider=llm_result.provider,
        llm_model=llm_result.model,
        graph_context=prompt.graph_context,
        # Pure provenance inspection for reporting; retrieval remains owned by M6.
        graph_triggered=graph_context.enabled and bool(extract_graph_refs(context)[0]),
    )


def _normalize_question(question: str) -> str:
    normalized_question = str(question or "").strip()
    if not normalized_question:
        raise BusinessError(
            RAG_QUERY_EMPTY,
            "Question must not be empty.",
            status_code=400,
        )
    return normalized_question


def _normalize_limit(limit: int | None, settings: Any) -> int:
    raw_limit = getattr(settings, "rag_top_k", 8) if limit is None else limit
    try:
        normalized_limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            RAG_CONFIG_INVALID,
            "RAG limit must be a positive integer.",
            detail={"limit": raw_limit},
            status_code=400,
        ) from exc

    if normalized_limit <= 0:
        raise BusinessError(
            RAG_CONFIG_INVALID,
            "RAG limit must be greater than 0.",
            detail={"limit": normalized_limit},
            status_code=400,
        )
    return normalized_limit
