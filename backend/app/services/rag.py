from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import logging
from typing import Any
from uuid import UUID

import app.services.hybrid_search as hybrid_search_service
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
from app.rag.context_builder import RagContext, RagContextStatus, build_rag_context
from app.graph.models import GraphTriggerProvenance, KGRef
from app.rag.graph_context_builder import GraphContext, build_graph_context
from app.rag.prompt import build_rag_prompt
from app.services.graph_retrieval import GraphRetrievalService
from app.services.hybrid_search import HybridSearchResult


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RagPrompt:
    system_prompt: str
    user_prompt: str


@dataclass(frozen=True)
class RagAnswerResult:
    question: str
    answer: str
    context_status: RagContextStatus
    citations: list[RagCitation]
    retrieval: HybridSearchResult
    llm_provider: str | None
    llm_model: str | None


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


def optional_rerank_chunks(question: str, search_result: HybridSearchResult, settings: Any) -> HybridSearchResult:
    del question
    if bool(getattr(settings, "reranker_enabled", False)):
        raise BusinessError(
            RAG_CONFIG_INVALID,
            "Reranker is reserved but not implemented in phase 6 minimal RAG.",
            status_code=400,
        )
    return search_result


def build_context(question: str, search_result: HybridSearchResult, settings: Any) -> RagContext:
    try:
        return build_rag_context(question, search_result, settings)
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
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            RAG_ANSWER_FAILED,
            "RAG prompt construction failed.",
            detail={"error_type": exc.__class__.__name__},
            status_code=500,
        ) from exc
    return RagPrompt(system_prompt=system_prompt, user_prompt=user_prompt)


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

    search_result = retrieve_chunks(
        db,
        normalized_question,
        normalized_limit,
        document_id,
        settings=settings,
    )
    search_result = optional_rerank_chunks(normalized_question, search_result, settings)
    context = build_context(normalized_question, search_result, settings)

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
