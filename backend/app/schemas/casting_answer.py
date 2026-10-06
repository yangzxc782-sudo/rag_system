"""Engineering publication has its own provenance contract, separate from RAG."""
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.conversation_persistence import PersistenceMetrics
from app.schemas.casting_storage import CastingErrorDetail

GENERATION_KEY = "casting_generation:v1"
RESULT_KEY = "casting_result:v1"
FactRef = Literal["candidate", "risers", "gating", "checks", "comparison", "rejected", "pending", "conversions", "error"]
ResultStatus = Literal["success", "no_feasible_candidate", "admission_failed", "engine_failed", "timed_out", "interrupted", "input_required", "clarify_selection"]
SummaryMode = Literal["llm_fact_refs", "llm_explanation", "template"]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class CastingNarrative(Closed):
    fact_refs: list[FactRef] = Field(max_length=9)

    @model_validator(mode="after")
    def unique(self):
        if len(self.fact_refs) != len(set(self.fact_refs)):
            raise ValueError("Duplicate fact references")
        return self


class CastingAnswerInfo(Closed):
    result_status: ResultStatus
    route: Literal["calculate", "explain_existing", "input_required", "clarify_selection"]
    run_id: UUID | None = None
    result_file_id: UUID | None = None
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_file_id: UUID | None = None
    input_reused: bool = False
    rule_id: str | None = None
    rule_version: str | None = None
    rule_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recommended_candidate_id: str | None = None
    candidate_count: int | None = Field(default=None, ge=0)
    candidate_rank: int | None = Field(default=None, ge=1, le=16)
    summary_mode: SummaryMode
    error: CastingErrorDetail | None = None


class CastingAnswerDraft(Closed):
    draft_type: Literal["casting_answer_v1"] = "casting_answer_v1"
    outcome: Literal["casting_design"] = "casting_design"
    text: str = Field(min_length=1, max_length=65536)
    route_artifact_id: UUID
    tool_artifact_id: UUID | None = None
    facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    renderer_version: Literal["casting_facts_v1", "casting_explanation_v1"] = "casting_facts_v1"
    narrative: CastingNarrative
    casting: CastingAnswerInfo


class CastingGenerationDetails(Closed):
    artifact_type: Literal["casting_generation"] = "casting_generation"
    route_artifact_id: UUID
    tool_artifact_id: UUID | None = None
    facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary_mode: SummaryMode
    answer_text_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metrics: PersistenceMetrics = Field(default_factory=PersistenceMetrics)


class CastingResultDetails(Closed):
    artifact_type: Literal["casting_result"] = "casting_result"
    outcome: Literal["casting_design"] = "casting_design"
    generation_artifact_id: UUID
    route_artifact_id: UUID


@dataclass(frozen=True)
class CastingPublicationProof:
    thread_id: UUID
    turn_id: UUID
    attempt_no: int
    snapshot_id: UUID
    draft: CastingAnswerDraft


@dataclass(frozen=True)
class StagedCastingResult:
    thread_id: UUID
    turn_id: UUID
    attempt_no: int
    result_artifact_id: UUID
    draft_snapshot_id: UUID
    proof: CastingPublicationProof
    outcome: Literal["casting_design"] = "casting_design"
    checkpoint_complete: bool = False
