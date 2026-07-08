from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.search import SearchData


RagContextStatus = Literal["ok", "no_context"]


class RagAskRequest(BaseModel):
    question: str
    limit: int = 8
    document_id: UUID | None = None


class RagCitationItem(BaseModel):
    citation_id: int
    chunk_id: str
    document_id: str
    original_filename: str | None = None
    chunk_index: int | None = None
    content: str
    hybrid_score: float | None = None
    retrieval_source: str | None = None

    @classmethod
    def from_service_citation(cls, citation: Any) -> "RagCitationItem":
        return cls(
            citation_id=int(getattr(citation, "citation_id")),
            chunk_id=str(getattr(citation, "chunk_id", "")),
            document_id=str(getattr(citation, "document_id", "")),
            original_filename=getattr(citation, "original_filename", None),
            chunk_index=getattr(citation, "chunk_index", None),
            content=str(getattr(citation, "content", "") or ""),
            hybrid_score=getattr(citation, "hybrid_score", None),
            retrieval_source=getattr(citation, "retrieval_source", None),
        )


class RagLlmInfo(BaseModel):
    provider: str | None = None
    model: str | None = None


class RagAskData(BaseModel):
    question: str
    answer: str
    context_status: RagContextStatus
    citations: list[RagCitationItem]
    retrieval: SearchData
    llm: RagLlmInfo

    @classmethod
    def from_service_result(cls, result: Any) -> "RagAskData":
        return cls(
            question=str(getattr(result, "question", "") or ""),
            answer=str(getattr(result, "answer", "") or ""),
            context_status=getattr(result, "context_status"),
            citations=[
                RagCitationItem.from_service_citation(citation)
                for citation in (getattr(result, "citations", []) or [])
            ],
            retrieval=SearchData.model_validate(getattr(result, "retrieval")),
            llm=RagLlmInfo(
                provider=getattr(result, "llm_provider", None),
                model=getattr(result, "llm_model", None),
            ),
        )
