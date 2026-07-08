from __future__ import annotations

from types import SimpleNamespace

from app.rag.citations import build_citations, validate_citation_ids
from app.rag.context_builder import TRUNCATION_MARKER, build_rag_context, format_context_for_prompt
from app.rag.prompt import build_rag_prompt, build_system_prompt


def make_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "rag_context_max_chars": 1200,
        "rag_no_context_message": "当前知识库中未检索到足够依据，无法可靠回答该问题。",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_item(
    chunk_id: str,
    *,
    hybrid_score: float,
    content: str,
    chunk_index: int = 1,
    retrieval_source: str = "both",
) -> SimpleNamespace:
    return SimpleNamespace(
        chunk_id=chunk_id,
        document_id=f"document-{chunk_id}",
        original_filename=f"{chunk_id}.txt",
        chunk_index=chunk_index,
        content=content,
        source_metadata={"parser": "fake"},
        retrieval_source=retrieval_source,
        keyword_score=1.5,
        vector_score=0.8,
        keyword_rank=1,
        vector_rank=1,
        hybrid_score=hybrid_score,
        matched_keywords=["冒口"],
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_dim=1024,
    )


def make_result(items: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(query="冒口如何保证热节补缩？", limit=8, total=len(items), items=items)


def test_empty_search_result_returns_no_context() -> None:
    context = build_rag_context("问题", make_result([]), make_settings())

    assert context.context_status == "no_context"
    assert context.chunks == []
    assert build_citations(context) == []
    assert format_context_for_prompt(context) == ""


def test_context_uses_hybrid_score_order_and_stable_citation_ids() -> None:
    result = make_result(
        [
            make_item("low", hybrid_score=0.1, content="低分片段"),
            make_item("high", hybrid_score=0.9, content="高分片段"),
            make_item("middle", hybrid_score=0.5, content="中分片段"),
        ]
    )

    context = build_rag_context("问题", result, make_settings())

    assert context.context_status == "ok"
    assert [chunk.chunk_id for chunk in context.chunks] == ["high", "middle", "low"]
    assert [chunk.citation_id for chunk in context.chunks] == [1, 2, 3]
    assert validate_citation_ids(context)


def test_citations_keep_source_and_scores_from_chunks() -> None:
    item = make_item("chunk-a", hybrid_score=0.42, content="冒口应服务于热节补缩", chunk_index=3)
    context = build_rag_context("问题", make_result([item]), make_settings())

    citations = build_citations(context)

    assert len(citations) == 1
    citation = citations[0]
    assert citation.citation_id == 1
    assert citation.chunk_id == "chunk-a"
    assert citation.document_id == "document-chunk-a"
    assert citation.original_filename == "chunk-a.txt"
    assert citation.chunk_index == 3
    assert citation.content == "冒口应服务于热节补缩"
    assert citation.hybrid_score == 0.42
    assert citation.retrieval_source == "both"


def test_context_length_is_limited_and_long_chunk_is_truncated() -> None:
    long_content = "冒口补缩" * 200
    settings = make_settings(rag_context_max_chars=260)

    context = build_rag_context("问题", make_result([make_item("long", hybrid_score=1.0, content=long_content)]), settings)
    formatted = format_context_for_prompt(context)

    assert context.context_status == "ok"
    assert len(formatted) <= settings.rag_context_max_chars
    assert context.chunks[0].was_truncated is True
    assert TRUNCATION_MARKER in context.chunks[0].content


def test_format_context_for_prompt_includes_citation_numbers() -> None:
    context = build_rag_context(
        "问题",
        make_result(
            [
                make_item("one", hybrid_score=0.9, content="片段一"),
                make_item("two", hybrid_score=0.8, content="片段二"),
            ]
        ),
        make_settings(),
    )
    formatted = format_context_for_prompt(context)

    assert "[1] 来源文件: one.txt" in formatted
    assert "[2] 来源文件: two.txt" in formatted
    assert "chunk_id: one" in formatted
    assert "chunk_id: two" in formatted


def test_prompt_contains_question_context_and_grounding_constraints() -> None:
    context = build_rag_context(
        "冒口如何保证热节补缩？",
        make_result([make_item("source", hybrid_score=0.9, content="冒口需要对热节形成有效补缩通道。")]),
        make_settings(),
    )

    system_prompt, user_prompt = build_rag_prompt("冒口如何保证热节补缩？", context, make_settings())

    assert "冒口如何保证热节补缩？" in user_prompt
    assert "冒口需要对热节形成有效补缩通道。" in user_prompt
    assert "只能依据" in system_prompt
    assert "不得脱离上下文编造" in system_prompt
    assert "当前知识库中未检索到足够依据，无法可靠回答该问题。" in system_prompt
    assert "API key" not in system_prompt
    assert "API key" not in user_prompt
    assert "api_key" not in system_prompt.lower()
    assert "api_key" not in user_prompt.lower()


def test_no_context_prompt_uses_no_context_text_without_exception() -> None:
    context = build_rag_context("问题", make_result([]), make_settings())

    system_prompt, user_prompt = build_rag_prompt("问题", context, make_settings())
    citations = build_citations(context)

    assert context.context_status == "no_context"
    assert citations == []
    assert "当前无可用检索上下文。" in user_prompt
    assert "当前知识库中未检索到足够依据，无法可靠回答该问题。" in system_prompt


def test_system_prompt_mentions_conflicting_chunks_need_manual_review() -> None:
    system_prompt = build_system_prompt(make_settings())

    assert "检索片段中存在不一致，需要人工核验" in system_prompt
