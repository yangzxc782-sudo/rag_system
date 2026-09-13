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


class RagGraphDocument(BaseModel):
    doc_id: str


class RagGraphTable(BaseModel):
    table_id: str
    table_ref: str
    page: int | None = None
    table_index: int | None = None


class RagGraphEntity(BaseModel):
    id: str
    name: str
    entity_type: str
    page: int | None = None


class RagGraphRelationship(BaseModel):
    source_entity_id: str
    source_name: str
    type: str
    target_entity_id: str
    target_name: str


class RagGraphEvidence(BaseModel):
    graph_id: str
    anchor_id: str
    anchor_type: str
    table_ref: str | None
    source_citations: list[int]
    document: RagGraphDocument
    table: RagGraphTable
    entities: list[RagGraphEntity]
    relationships: list[RagGraphRelationship]


class RagGraphData(BaseModel):
    enabled: bool
    triggered: bool
    status: Literal["success", "partial", "not_triggered", "unavailable"]
    truncated: bool
    evidence_count: int
    evidence: list[RagGraphEvidence]


class RagAskData(BaseModel):
    question: str
    answer: str
    context_status: RagContextStatus
    citations: list[RagCitationItem]
    retrieval: SearchData
    llm: RagLlmInfo
    graph: RagGraphData | None = None

    @classmethod
    def from_service_result(cls, result: Any) -> "RagAskData":
        from app.rag.graph_response import build_graph_response

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
            graph=build_graph_response(
                getattr(result, "graph_context", None),
                triggered=bool(getattr(result, "graph_triggered", False)),
            ),
        )
