from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.services.rag as rag_service
from app.retrieval.embeddings import EmbeddingResult
from app.core.errors import (
    LLM_GENERATION_FAILED,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    RAG_CONFIG_INVALID,
    RAG_QUERY_EMPTY,
    BusinessError,
)
from app.llm.messages import LLMMessage, LLMTextContentPart
from app.llm.provider import LLMGenerateResult


class FakeLLMProvider:
    provider_name = "fake"

    def __init__(self, text: str = "grounded answer", exc: BusinessError | None = None) -> None:
        self.text = text
        self.exc = exc
        self.calls: list[object] = []

    def generate(self, request: object) -> LLMGenerateResult | SimpleNamespace:
        self.calls.append(request)
        if self.exc is not None:
            raise self.exc
        if not self.text.strip():
            return SimpleNamespace(
                text=self.text,
                provider="fake",
                model="fake-model",
            )
        return LLMGenerateResult(
            message=LLMMessage(
                role="assistant",
                content=(LLMTextContentPart(text=self.text),),
            ),
            provider="fake",
            model="fake-model",
        )


class FakeSDKCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            id="chatcmpl-rag-business",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content="cached provider answer",
                        tool_calls=None,
                    )
                )
            ],
            usage=None,
        )


class FakeSDKClient:
    def __init__(self) -> None:
        self.completions = FakeSDKCompletions()
        self.chat = SimpleNamespace(completions=self.completions)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def make_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "rag_top_k": 8,
        "rag_context_max_chars": 1200,
        "rag_no_context_message": "No sufficient context.",
        "llm_provider": "openai_compatible",
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "qwen3:8b",
        "llm_api_key": "",
        "llm_temperature": 0.2,
        "llm_max_tokens": 2048,
        "llm_timeout_seconds": 120,
        "llm_remote_model": "remote-model",
        "reranker_enabled": False,
        "app_name": "test-rag-app",
        "backend_cors_origin_list": ["http://localhost:3000"],
        "api_v1_prefix": "/api/v1",
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


def test_empty_retrieval_returns_no_context_without_calling_llm(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    result = make_result(items=[])
    patch_hybrid_search(monkeypatch, result)
    llm_provider = FakeLLMProvider()
    settings = make_settings(rag_no_context_message="No context available.")
    caplog.set_level(logging.INFO, logger="app.llm")

    answer = rag_service.answer_question(object(), "question", settings=settings, llm_provider=llm_provider)

    assert answer.context_status == "no_context"
    assert answer.answer == "No context available."
    assert answer.citations == []
    assert answer.retrieval.total == 0
    assert answer.retrieval.items == []
    assert llm_provider.calls == []
    assert not any(
        str(getattr(record, "event", "")).startswith("llm_generation")
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("configured_provider", "expected_provider", "expected_model"),
    [
        ("local", "local", "qwen3:8b"),
        ("api", "api", "remote-model"),
        ("openai_compatible", "local", "qwen3:8b"),
    ],
)
def test_no_context_uses_normalized_metadata_without_provider_or_cache(
    monkeypatch: pytest.MonkeyPatch,
    configured_provider: str,
    expected_provider: str,
    expected_model: str,
) -> None:
    import app.llm.openai_chat_transport as transport_module
    import app.llm.provider as provider_module

    provider_module.clear_llm_provider_cache()
    patch_hybrid_search(monkeypatch, make_result(items=[]))

    def unexpected_get() -> object:
        raise AssertionError("no-context must not get an LLM provider")

    def unexpected_build(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("no-context must not build an LLM provider")

    def unexpected_client(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("no-context must not create an OpenAI client")

    monkeypatch.setattr(rag_service, "get_llm_provider", unexpected_get)
    monkeypatch.setattr(provider_module, "build_llm_provider", unexpected_build)
    monkeypatch.setattr(
        transport_module,
        "_default_openai_client_factory",
        unexpected_client,
    )

    answer = rag_service.answer_question(
        object(),
        "question",
        settings=make_settings(llm_provider=configured_provider),
    )

    assert answer.context_status == "no_context"
    assert answer.llm_provider == expected_provider
    assert answer.llm_model == expected_model
    assert provider_module._provider_cache is None


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
    assert [message.role for message in request.messages] == ["system", "user"]
    assert "你是铸型工艺知识库问答助手" in request.messages[0].content[0].text
    assert "question" in request.messages[1].content[0].text
    assert "chunk-b" in request.messages[1].content[0].text


def test_rag_llm_runs_after_deletion_filter_session_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hybrid_module = rag_service.hybrid_search_service
    document_id = uuid4()
    chunk_id = uuid4()
    events: list[str] = []

    class QueryEmbeddingProvider:
        def encode_query(self, _query: str) -> EmbeddingResult:
            return EmbeddingResult(
                embeddings=[[0.1] * 1024],
                embedding_model="Qwen3-Embedding-0.6B",
                embedding_dim=1024,
                device="cpu",
                provider="fake",
            )

    class SearchClient:
        def search(self, **_kwargs: object) -> dict[str, object]:
            return {
                "hits": {
                    "hits": [
                        {
                            "_id": str(chunk_id),
                            "_score": 1.0,
                            "_source": {
                                "chunk_id": str(chunk_id),
                                "document_id": str(document_id),
                                "original_filename": "casting.md",
                                "chunk_index": 0,
                                "content": "Riser feeding path.",
                                "source_metadata": {},
                                "exact_terms": ["riser"],
                                "embedding_model": "Qwen3-Embedding-0.6B",
                                "embedding_dim": 1024,
                            },
                        }
                    ]
                }
            }

    class FilterSession:
        closed = False

        def scalars(self, _statement: object) -> SimpleNamespace:
            events.append("filter-select")
            return SimpleNamespace(all=lambda: [document_id])

        def close(self) -> None:
            self.closed = True
            events.append("filter-close")

    filter_session = FilterSession()

    class AssertClosedLLM(FakeLLMProvider):
        def generate(self, request: object) -> LLMGenerateResult | SimpleNamespace:
            assert filter_session.closed is True
            events.append("llm")
            return super().generate(request)

    original_fuse = hybrid_module.fuse_hybrid_results

    def tracked_fuse(*args: object, **kwargs: object):
        assert filter_session.closed is True
        events.append("rrf")
        return original_fuse(*args, **kwargs)

    monkeypatch.setattr(
        hybrid_module,
        "SessionLocal",
        lambda: (events.append("filter-open"), filter_session)[1],
        raising=False,
    )
    monkeypatch.setattr(
        hybrid_module,
        "get_embedding_provider",
        lambda _settings: QueryEmbeddingProvider(),
    )
    monkeypatch.setattr(
        hybrid_module,
        "get_search_engine_client",
        lambda _settings: SearchClient(),
    )
    monkeypatch.setattr(hybrid_module, "fuse_hybrid_results", tracked_fuse)
    settings = make_settings(
        search_index_alias="casting_chunks_current",
        search_query_analyzer="ik_smart",
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_dim=1024,
        hybrid_keyword_weight=0.5,
        hybrid_vector_weight=0.5,
        hybrid_rrf_k=60,
        hybrid_keyword_top_k=50,
        hybrid_vector_top_k=50,
    )

    answer = rag_service.answer_question(
        SimpleNamespace(
            scalars=lambda _statement: pytest.fail(
                "request-scoped Session must not filter deletion status"
            )
        ),
        "How should the riser feed the hot spot?",
        settings=settings,
        llm_provider=AssertClosedLLM(),
    )

    assert answer.context_status == "ok"
    assert events == ["filter-open", "filter-select", "filter-close", "rrf", "llm"]


def test_generate_answer_uses_from_prompt_as_the_only_request_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    class RequestFactorySpy:
        @classmethod
        def from_prompt(
            cls,
            prompt: str,
            system_prompt: str | None = None,
            **kwargs: object,
        ) -> object:
            captured.append(
                {
                    "prompt": prompt,
                    "system_prompt": system_prompt,
                    **kwargs,
                }
            )
            return SimpleNamespace()

    monkeypatch.setattr(rag_service, "LLMGenerateRequest", RequestFactorySpy)
    provider = FakeLLMProvider()

    rag_service.generate_answer(
        rag_service.RagPrompt(
            system_prompt="system rules",
            user_prompt="grounded question",
        ),
        provider,
        make_settings(),
    )

    assert captured == [
        {
            "prompt": "grounded question",
            "system_prompt": "system rules",
            "temperature": 0.2,
            "max_tokens": 2048,
        }
    ]


def test_answer_question_uses_no_arg_provider_getter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_hybrid_search(monkeypatch, make_result(items=[make_item()]))
    provider = FakeLLMProvider()
    get_calls = 0

    def fake_get_provider() -> FakeLLMProvider:
        nonlocal get_calls
        get_calls += 1
        return provider

    monkeypatch.setattr(rag_service, "get_llm_provider", fake_get_provider)

    rag_service.answer_question(object(), "question", settings=make_settings())

    assert get_calls == 1
    assert len(provider.calls) == 1


def test_business_calls_reuse_cached_provider_and_shutdown_closes_its_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.llm.openai_chat_transport as transport_module
    import app.llm.provider as provider_module
    from app.main import create_app

    settings = make_settings(llm_provider="local")
    patch_hybrid_search(monkeypatch, make_result(items=[make_item()]))
    created_clients: list[FakeSDKClient] = []

    def client_factory(**_kwargs: object) -> FakeSDKClient:
        client = FakeSDKClient()
        created_clients.append(client)
        return client

    provider_module.clear_llm_provider_cache()
    monkeypatch.setattr(provider_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        transport_module,
        "_default_openai_client_factory",
        client_factory,
    )

    try:
        first = rag_service.answer_question(object(), "question one", settings=settings)
        second = rag_service.answer_question(object(), "question two", settings=settings)

        assert first.answer == "cached provider answer"
        assert second.answer == "cached provider answer"
        assert len(created_clients) == 1
        assert len(created_clients[0].completions.calls) == 2
        assert provider_module._provider_cache is not None

        app = create_app(settings=settings)
        with TestClient(app):
            pass

        assert created_clients[0].close_calls == 1
        assert provider_module._provider_cache is None
    finally:
        provider_module.clear_llm_provider_cache()


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
    assert "generate_document_embeddings" not in source
    assert "create_or_update_search_index" not in source
    assert "sync_document_chunks" not in source
    assert "rebuild" not in source.lower()
