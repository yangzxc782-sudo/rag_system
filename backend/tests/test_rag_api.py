from __future__ import annotations

import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import rag as rag_api
from app.api.v1.router import api_router
from app.core.errors import (
    HYBRID_SEARCH_FAILED,
    LLM_AUTHENTICATION_FAILED,
    LLM_CONFIG_INVALID,
    LLM_EMPTY_CONTENT,
    LLM_GENERATION_FAILED,
    LLM_JSON_INVALID,
    LLM_MODEL_NOT_FOUND,
    LLM_PARAMETER_UNSUPPORTED,
    LLM_PERMISSION_DENIED,
    LLM_PROVIDER_INVALID,
    LLM_RATE_LIMITED,
    LLM_REQUEST_INVALID,
    LLM_REQUEST_REJECTED,
    LLM_RESPONSE_INVALID,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    LLM_UPSTREAM_FAILED,
    RAG_ANSWER_FAILED,
    RAG_CONFIG_INVALID,
    RAG_QUERY_EMPTY,
    SEARCH_ENGINE_UNAVAILABLE,
    SEARCH_INDEX_NOT_FOUND,
    BusinessError,
)


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[rag_api.get_db] = lambda: object()
    return TestClient(app)


def make_search_item(
    chunk_id: str | None = None,
    document_id: str | None = None,
    *,
    hybrid_score: float = 0.9,
) -> SimpleNamespace:
    return SimpleNamespace(
        chunk_id=chunk_id or str(uuid4()),
        document_id=document_id or str(uuid4()),
        original_filename="source.txt",
        chunk_index=3,
        content="Riser feeding evidence.",
        source_metadata={"parser": "fake"},
        retrieval_source="both",
        keyword_score=1.2,
        vector_score=0.8,
        keyword_rank=1,
        vector_rank=1,
        hybrid_score=hybrid_score,
        matched_keywords=["riser"],
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_dim=1024,
    )


def make_service_result(context_status: str = "ok", items: list[SimpleNamespace] | None = None) -> SimpleNamespace:
    items = items if items is not None else [make_search_item()]
    citations = []
    if context_status == "ok":
        for citation_id, item in enumerate(items, start=1):
            citations.append(
                SimpleNamespace(
                    citation_id=citation_id,
                    chunk_id=str(item.chunk_id),
                    document_id=str(item.document_id),
                    original_filename=item.original_filename,
                    chunk_index=item.chunk_index,
                    content=item.content,
                    hybrid_score=item.hybrid_score,
                    retrieval_source=item.retrieval_source,
                )
            )

    return SimpleNamespace(
        question="How should a riser feed a hot spot?",
        answer="Use the cited chunks.",
        context_status=context_status,
        citations=citations,
        retrieval=SimpleNamespace(
            query="How should a riser feed a hot spot?",
            limit=8,
            total=len(items),
            items=items,
        ),
        llm_provider="openai_compatible",
        llm_model="qwen3:8b",
    )


def patch_answer_question(monkeypatch: pytest.MonkeyPatch, result: SimpleNamespace) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_answer_question(db: object, **kwargs: object) -> SimpleNamespace:
        calls.append({"db": db, **kwargs})
        return result

    monkeypatch.setattr(rag_api.rag_service, "answer_question", fake_answer_question)
    return calls


def test_rag_ask_success_returns_answer_citations_retrieval_and_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    result = make_service_result()
    calls = patch_answer_question(monkeypatch, result)
    response = make_client().post(
        "/api/v1/rag/ask",
        json={"question": "How should a riser feed a hot spot?", "limit": 8, "document_id": None},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["error"] is None
    assert payload["data"]["question"] == "How should a riser feed a hot spot?"
    assert payload["data"]["answer"] == "Use the cited chunks."
    assert payload["data"]["context_status"] == "ok"
    assert len(payload["data"]["citations"]) == 1
    assert payload["data"]["retrieval"]["total"] == 1
    assert payload["data"]["llm"] == {"provider": "openai_compatible", "model": "qwen3:8b"}
    assert len(calls) == 1
    assert calls[0]["question"] == "How should a riser feed a hot spot?"
    assert calls[0]["limit"] == 8
    assert calls[0]["document_id"] is None


def test_rag_ask_passes_document_id_to_service(monkeypatch: pytest.MonkeyPatch) -> None:
    document_id = str(uuid4())
    calls = patch_answer_question(monkeypatch, make_service_result())

    response = make_client().post(
        "/api/v1/rag/ask",
        json={"question": "question", "limit": 5, "document_id": document_id},
    )

    assert response.status_code == 200
    assert str(calls[0]["document_id"]) == document_id
    assert calls[0]["limit"] == 5


def test_rag_ask_no_context_returns_http_200_success(monkeypatch: pytest.MonkeyPatch) -> None:
    result = make_service_result(context_status="no_context", items=[])
    result.answer = "No sufficient context."
    patch_answer_question(monkeypatch, result)

    response = make_client().post("/api/v1/rag/ask", json={"question": "unknown", "limit": 8})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["context_status"] == "no_context"
    assert payload["data"]["answer"] == "No sufficient context."
    assert payload["data"]["citations"] == []
    assert payload["data"]["retrieval"]["total"] == 0
    assert payload["data"]["retrieval"]["items"] == []


def test_blank_question_returns_rag_query_empty() -> None:
    response = make_client().post("/api/v1/rag/ask", json={"question": "   ", "limit": 8})

    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == RAG_QUERY_EMPTY


def test_invalid_limit_returns_rag_config_invalid() -> None:
    response = make_client().post("/api/v1/rag/ask", json={"question": "question", "limit": 0})

    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == RAG_CONFIG_INVALID


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        (LLM_CONFIG_INVALID, 400),
        (LLM_PROVIDER_INVALID, 400),
        (LLM_REQUEST_INVALID, 400),
        (LLM_PARAMETER_UNSUPPORTED, 400),
        (LLM_AUTHENTICATION_FAILED, 502),
        (LLM_PERMISSION_DENIED, 502),
        (LLM_MODEL_NOT_FOUND, 502),
        (LLM_RATE_LIMITED, 429),
        (LLM_REQUEST_REJECTED, 502),
        (LLM_UPSTREAM_FAILED, 502),
        (LLM_RESPONSE_INVALID, 502),
        (LLM_EMPTY_CONTENT, 502),
        (LLM_JSON_INVALID, 502),
        (LLM_UNAVAILABLE, 503),
        (LLM_TIMEOUT, 504),
        (LLM_GENERATION_FAILED, 500),
        (RAG_CONFIG_INVALID, 400),
        (RAG_ANSWER_FAILED, 500),
        (SEARCH_ENGINE_UNAVAILABLE, 503),
        (SEARCH_INDEX_NOT_FOUND, 404),
        (HYBRID_SEARCH_FAILED, 500),
    ],
)
def test_rag_ask_maps_business_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    expected_status: int,
) -> None:
    def fake_answer_question(_db: object, **_kwargs: object) -> object:
        raise BusinessError(error_code, "failed", status_code=500)

    monkeypatch.setattr(rag_api.rag_service, "answer_question", fake_answer_question)

    response = make_client().post("/api/v1/rag/ask", json={"question": "question", "limit": 8})

    assert response.status_code == expected_status
    payload = response.json()
    assert payload["success"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == error_code


def test_router_registers_rag_without_removing_search_routes() -> None:
    from app.main import app

    paths = set(app.openapi().get("paths", {}).keys())

    assert "/api/v1/rag/ask" in paths
    assert "/api/v1/search" in paths
    assert "/api/v1/search/vector" in paths


def test_rag_openapi_request_schema_does_not_add_messages_or_history() -> None:
    schemas = make_client().app.openapi()["components"]["schemas"]
    schema = schemas["RagAskRequest"]

    assert set(schema["properties"]) == {"question", "limit", "document_id"}
    assert "messages" not in schema["properties"]
    assert "history" not in schema["properties"]
    assert set(schemas["RagAskData"]["properties"]) == {
        "question",
        "answer",
        "context_status",
        "citations",
        "retrieval",
        "llm",
    }


def test_rag_api_does_not_call_search_or_log_directly() -> None:
    source = inspect.getsource(rag_api)

    assert "hybrid_search_chunks" not in source
    assert "retrieval_logs" not in source
    assert "opensearch" not in source.lower()
