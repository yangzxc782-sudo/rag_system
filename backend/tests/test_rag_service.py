from __future__ import annotations

import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.services.rag as rag_service
from app.core.errors import (
    LLM_GENERATION_FAILED,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    RAG_CONFIG_INVALID,
    RAG_QUERY_EMPTY,
    BusinessError,
)
from app.llm.provider import LLMGenerateResult


class FakeLLMProvider:
    provider_name = "fake"

    def __init__(self, text: str = "grounded answer", exc: BusinessError | None = None) -> None:
        self.text = text
        self.exc = exc
        self.calls: list[object] = []

    def generate(self, request: object) -> LLMGenerateResult:
        self.calls.append(request)
        if self.exc is not None:
            raise self.exc
        return LLMGenerateResult(text=self.text, provider="fake", model="fake-model")


def make_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "rag_top_k": 8,
        "rag_context_max_chars": 1200,
        "rag_no_context_message": "No sufficient context.",
        "llm_provider": "openai_compatible",
        "llm_model": "qwen3:8b",
        "llm_temperature": 0.2,
        "llm_max_tokens": 2048,
        "reranker_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_item(
    chunk_id: str = "chunk-1",
    *,
    hybrid_score: float = 0.9,
    content: str = "The riser should feed the hot spot through an effective path.",
) -> SimpleNamespace:
    return SimpleNamespace(
        chunk_id=chunk_id,
        document_id=f"document-{chunk_id}",
        original_filename=f"{chunk_id}.txt",
        chunk_index=1,
        content=content,
        source_metadata={"parser": "fake"},
        retrieval_source="both",
        keyword_score=1.0,
        vector_score=0.8,
        keyword_rank=1,
        vector_rank=1,
        hybrid_score=hybrid_score,
        matched_keywords=["riser"],
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_dim=1024,
    )


def make_result(query: str = "question", limit: int = 8, items: list[SimpleNamespace] | None = None) -> SimpleNamespace:
    items = items or []
    return SimpleNamespace(query=query, limit=limit, total=len(items), items=items)


def patch_hybrid_search(monkeypatch: pytest.MonkeyPatch, result: SimpleNamespace) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_hybrid_search_chunks(db: object, **kwargs: object) -> SimpleNamespace:
        calls.append({"db": db, **kwargs})
        return result

    monkeypatch.setattr(rag_service.hybrid_search_service, "hybrid_search_chunks", fake_hybrid_search_chunks)
    return calls


def test_answer_question_calls_hybrid_search_and_passes_limit_and_document_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = make_result(items=[make_item()])
    calls = patch_hybrid_search(monkeypatch, result)
    document_id = uuid4()
    settings = make_settings()

    rag_service.answer_question(
        object(),
        "  How should a riser feed a hot spot?  ",
        limit=5,
        document_id=document_id,
        settings=settings,
        llm_provider=FakeLLMProvider(),
    )

    assert len(calls) == 1
    assert calls[0]["query"] == "How should a riser feed a hot spot?"
    assert calls[0]["limit"] == 5
    assert calls[0]["document_id"] == document_id
    assert calls[0]["settings"] is settings


def test_answer_question_uses_settings_limit_when_limit_is_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    result = make_result(items=[make_item()])
    calls = patch_hybrid_search(monkeypatch, result)
    settings = make_settings(rag_top_k=6)

    rag_service.answer_question(object(), "question", settings=settings, llm_provider=FakeLLMProvider())

    assert calls[0]["limit"] == 6


def test_blank_question_raises_rag_query_empty() -> None:
    with pytest.raises(BusinessError) as exc_info:
        rag_service.answer_question(object(), "   ", settings=make_settings(), llm_provider=FakeLLMProvider())

    assert exc_info.value.code == RAG_QUERY_EMPTY


def test_invalid_limit_raises_rag_config_invalid() -> None:
    with pytest.raises(BusinessError) as exc_info:
        rag_service.answer_question(object(), "question", limit=0, settings=make_settings(), llm_provider=FakeLLMProvider())

    assert exc_info.value.code == RAG_CONFIG_INVALID


def test_empty_retrieval_returns_no_context_without_calling_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    result = make_result(items=[])
    patch_hybrid_search(monkeypatch, result)
    llm_provider = FakeLLMProvider()
    settings = make_settings(rag_no_context_message="No context available.")

    answer = rag_service.answer_question(object(), "question", settings=settings, llm_provider=llm_provider)

    assert answer.context_status == "no_context"
    assert answer.answer == "No context available."
    assert answer.citations == []
    assert answer.retrieval.total == 0
    assert answer.retrieval.items == []
    assert llm_provider.calls == []


def test_answer_question_with_context_calls_llm_and_returns_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    result = make_result(items=[make_item("chunk-a", hybrid_score=0.7), make_item("chunk-b", hybrid_score=0.9)])
    patch_hybrid_search(monkeypatch, result)
    llm_provider = FakeLLMProvider(text="Use the cited chunks.")

    answer = rag_service.answer_question(object(), "question", settings=make_settings(), llm_provider=llm_provider)

    assert answer.context_status == "ok"
    assert answer.answer == "Use the cited chunks."
    assert answer.llm_provider == "fake"
    assert answer.llm_model == "fake-model"
    assert [citation.citation_id for citation in answer.citations] == [1, 2]
    assert [citation.chunk_id for citation in answer.citations] == ["chunk-b", "chunk-a"]
    assert len(llm_provider.calls) == 1
    request = llm_provider.calls[0]
    assert getattr(request, "system_prompt")
    assert "question" in getattr(request, "prompt")
    assert "chunk-b" in getattr(request, "prompt")


def test_optional_rerank_chunks_is_noop_when_disabled() -> None:
    result = make_result(items=[make_item()])

    reranked = rag_service.optional_rerank_chunks("question", result, make_settings(reranker_enabled=False))

    assert reranked is result


def test_optional_rerank_chunks_rejects_enabled_reranker() -> None:
    with pytest.raises(BusinessError) as exc_info:
        rag_service.optional_rerank_chunks("question", make_result(), make_settings(reranker_enabled=True))

    assert exc_info.value.code == RAG_CONFIG_INVALID


@pytest.mark.parametrize("error_code", [LLM_UNAVAILABLE, LLM_TIMEOUT, LLM_GENERATION_FAILED])
def test_llm_business_errors_propagate(monkeypatch: pytest.MonkeyPatch, error_code: str) -> None:
    result = make_result(items=[make_item()])
    patch_hybrid_search(monkeypatch, result)
    llm_provider = FakeLLMProvider(exc=BusinessError(error_code, "LLM failed.", status_code=500))

    with pytest.raises(BusinessError) as exc_info:
        rag_service.answer_question(object(), "question", settings=make_settings(), llm_provider=llm_provider)

    assert exc_info.value.code == error_code


def test_empty_llm_text_maps_to_llm_generation_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    result = make_result(items=[make_item()])
    patch_hybrid_search(monkeypatch, result)

    with pytest.raises(BusinessError) as exc_info:
        rag_service.answer_question(object(), "question", settings=make_settings(), llm_provider=FakeLLMProvider(text=" "))

    assert exc_info.value.code == LLM_GENERATION_FAILED


def test_service_does_not_reference_retrieval_logs_or_langgraph() -> None:
    source = inspect.getsource(rag_service)

    assert "retrieval_logs" not in source
    assert "langgraph" not in source.lower()
