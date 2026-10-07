"""Closed stage metadata. All retrieved/generated text belongs to snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.rag.citations import RagCitation
from app.rag.context_builder import RagContextChunk
from app.rag.graph_context_builder import GraphContext, GraphEvidence, GraphDiagnostic
from app.schemas.conversation_persistence import PersistenceMetrics
from app.services.hybrid_search import HybridSearchItem


Key = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")]
Outcome = Literal["answer", "no_context", "clarification"]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class RetrievalDetails(ClosedModel):
    artifact_type: Literal["chat_retrieval_v1"] = "chat_retrieval_v1"
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(ge=1, le=50)
    document_id: UUID | None = None
    evidence_generation: int = Field(ge=1)
    hybrid_limit: int = Field(ge=1, le=50)
    candidate_limit: int | None = Field(default=None, ge=1, le=50)
    hybrid_count: int = Field(ge=0, le=50)
    rerank_applied: bool
    fallback_reason: Key | None = None
    retrieval_ms: int = Field(ge=0)
    rerank_ms: int = Field(ge=0)
    snapshot_keys: list[Key] = Field(max_length=50)


class EvidenceDetails(ClosedModel):
    artifact_type: Literal["chat_evidence_v1"] = "chat_evidence_v1"
    evidence_generation: int = Field(ge=1)
    citation_keys: list[Key] = Field(max_length=50)
    graph_keys: list[Key] = Field(max_length=50)
    history_message_ids: list[UUID] = Field(max_length=12)
    graph_enabled: bool
    graph_triggered: bool
    graph_truncated: bool
    graph_schema_version: Literal[2] | None = None
    graph_diagnostics: tuple[GraphDiagnostic, ...] = ()
    graph_source_error: Key | None = None
    input_estimated_tokens: int = Field(ge=0)
    budget_basis: Literal["estimated_utf8_bytes_v1"] = "estimated_utf8_bytes_v1"
    prompt_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class GenerationDetails(ClosedModel):
    artifact_type: Literal["chat_generation_v1"] = "chat_generation_v1"
    outcome: Outcome
    snapshot_keys: list[Literal["answer"]] = Field(min_length=1, max_length=1)
    metrics: PersistenceMetrics = Field(default_factory=PersistenceMetrics)


class ResultDetails(ClosedModel):
    artifact_type: Literal["chat_result_v1"] = "chat_result_v1"
    outcome: Outcome
    generation_artifact_id: UUID
    evidence_artifact_id: UUID | None = None


StageDetails = RetrievalDetails | EvidenceDetails | GenerationDetails | ResultDetails
STAGE_ADAPTER = TypeAdapter(Annotated[StageDetails, Field(discriminator="artifact_type")])
STAGE_KINDS = {"chat_retrieval_v1": "retrieval", "chat_evidence_v1": "evidence",
               "chat_generation_v1": "generation", "chat_result_v1": "result"}


class CandidatePayload(ClosedModel):
    item: HybridSearchItem


class CitationPayload(ClosedModel):
    chunk: RagContextChunk


class GraphPayload(ClosedModel):
    schema_version: Literal[2] = 2
    evidence: GraphEvidence


@dataclass(frozen=True)
class StagedConversationResult:
    thread_id: UUID
    turn_id: UUID
    attempt_no: int
    result_artifact_id: UUID
    draft_snapshot_id: UUID
    question: str
    answer: str
    outcome: Outcome
    citations: tuple[RagCitation, ...]
    graph_context: GraphContext
    llm_provider: str | None
    llm_model: str | None
    checkpoint_complete: bool = False


def read_graph_payload(payload: dict) -> GraphEvidence:
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 2:
        from app.services.conversation_repository import ConversationError
        raise ConversationError("QA_GRAPH_EVIDENCE_VERSION_UNSUPPORTED",
            "旧版图谱证据仅保留为历史记录，无法恢复执行；请发起新一轮检索。", status_code=409)
    import json
    return GraphPayload.model_validate_json(json.dumps(payload)).evidence
