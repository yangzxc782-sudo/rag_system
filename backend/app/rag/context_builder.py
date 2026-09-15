from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


RagContextStatus = Literal["ok", "no_context"]
TRUNCATION_MARKER = "……[内容已截断]"
NO_CONTEXT_TEXT = "当前无可用检索上下文。"


@dataclass(frozen=True)
class RagContextChunk:
    citation_id: int
    chunk_id: str
    document_id: str
    original_filename: str
    chunk_index: int
    content: str
    source_metadata: dict[str, Any] | None
    retrieval_source: str
    hybrid_score: float
    keyword_score: float | None
    vector_score: float | None
    was_truncated: bool = False


@dataclass(frozen=True)
class RagContext:
    question: str
    context_status: RagContextStatus
    chunks: list[RagContextChunk]
    max_chars: int
    total_chars: int


def build_rag_context(
    question: str, search_result: Any, settings: Any, *, preserve_order: bool = False,
) -> RagContext:
    max_chars = int(getattr(settings, "rag_context_max_chars", 12000))
    sorted_items = list(getattr(search_result, "items", []) or [])
    if not preserve_order:
        sorted_items.sort(
            key=lambda item: float(getattr(item, "hybrid_score", 0.0) or 0.0),
            reverse=True,
        )

    if not sorted_items:
        return RagContext(
            question=question,
            context_status="no_context",
            chunks=[],
            max_chars=max_chars,
            total_chars=0,
        )

    chunks: list[RagContextChunk] = []
    used_chars = 0
    safe_max_chars = max(0, max_chars)

    for item in sorted_items:
        citation_id = len(chunks) + 1
        content = str(getattr(item, "content", "") or "")
        base_chunk = _build_context_chunk(item, citation_id=citation_id, content="", was_truncated=False)
        empty_block_len = len(_format_chunk_block(base_chunk))
        separator_len = 2 if chunks else 0
        remaining = safe_max_chars - used_chars - separator_len
        if remaining <= empty_block_len:
            break

        content_budget = remaining - empty_block_len
        chunk_content, was_truncated = truncate_content(content, content_budget)
        chunk = _build_context_chunk(
            item,
            citation_id=citation_id,
            content=chunk_content,
            was_truncated=was_truncated,
        )
        block_len = separator_len + len(_format_chunk_block(chunk))
        if used_chars + block_len > safe_max_chars:
            break

        chunks.append(chunk)
        used_chars += block_len

    if not chunks:
        return RagContext(
            question=question,
            context_status="no_context",
            chunks=[],
            max_chars=max_chars,
            total_chars=0,
        )

    return RagContext(
        question=question,
        context_status="ok",
        chunks=chunks,
        max_chars=max_chars,
        total_chars=used_chars,
    )


def truncate_content(content: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        return "", bool(content)
    if len(content) <= max_chars:
        return content, False
    if max_chars <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:max_chars], True
    return f"{content[: max_chars - len(TRUNCATION_MARKER)]}{TRUNCATION_MARKER}", True


def format_context_for_prompt(context: RagContext) -> str:
    if context.context_status == "no_context" or not context.chunks:
        return ""
    return "\n\n".join(_format_chunk_block(chunk) for chunk in context.chunks)


def _build_context_chunk(
    item: Any,
    *,
    citation_id: int,
    content: str,
    was_truncated: bool,
) -> RagContextChunk:
    return RagContextChunk(
        citation_id=citation_id,
        chunk_id=str(getattr(item, "chunk_id", "") or ""),
        document_id=str(getattr(item, "document_id", "") or ""),
        original_filename=str(getattr(item, "original_filename", "") or ""),
        chunk_index=int(getattr(item, "chunk_index", 0) or 0),
        content=content,
        source_metadata=getattr(item, "source_metadata", None),
        retrieval_source=str(getattr(item, "retrieval_source", "") or ""),
        hybrid_score=float(getattr(item, "hybrid_score", 0.0) or 0.0),
        keyword_score=getattr(item, "keyword_score", None),
        vector_score=getattr(item, "vector_score", None),
        was_truncated=was_truncated,
    )


def _format_chunk_block(chunk: RagContextChunk) -> str:
    return "\n".join(
        [
            f"[{chunk.citation_id}] 来源文件: {chunk.original_filename}",
            f"chunk_index: {chunk.chunk_index}",
            f"document_id: {chunk.document_id}",
            f"chunk_id: {chunk.chunk_id}",
            f"retrieval_source: {chunk.retrieval_source}",
            f"hybrid_score: {chunk.hybrid_score:.6f}",
            "content:",
            chunk.content,
        ]
    )
