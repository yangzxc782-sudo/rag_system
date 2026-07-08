from __future__ import annotations

from dataclasses import dataclass

from app.rag.context_builder import RagContext


@dataclass(frozen=True)
class RagCitation:
    citation_id: int
    chunk_id: str
    document_id: str
    original_filename: str
    chunk_index: int
    content: str
    hybrid_score: float
    retrieval_source: str


def build_citations(context: RagContext) -> list[RagCitation]:
    if context.context_status == "no_context":
        return []
    return [
        RagCitation(
            citation_id=chunk.citation_id,
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            original_filename=chunk.original_filename,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            hybrid_score=chunk.hybrid_score,
            retrieval_source=chunk.retrieval_source,
        )
        for chunk in context.chunks
    ]


def validate_citation_ids(context: RagContext) -> bool:
    expected_ids = list(range(1, len(context.chunks) + 1))
    actual_ids = [chunk.citation_id for chunk in context.chunks]
    return actual_ids == expected_ids
