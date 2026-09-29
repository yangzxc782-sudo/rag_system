"""Closed LLM rewrite v2 contracts; provenance is not an edit script."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.conversation_persistence import PersistenceMetrics

REWRITE_STRATEGY = "llm_v2"
REWRITE_ARTIFACT_KEY = "query_rewrite:v2"
REWRITE_POLICY_KEY = "query_rewrite_policy:v2"


class ResolvedReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    surface: str = Field(max_length=100)
    referent: str = Field(min_length=1, max_length=100)
    source_message_ids: list[UUID] = Field(min_length=1, max_length=13)

    @model_validator(mode="after")
    def valid_sources(self):
        if not self.referent.strip() or len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("Invalid reference metadata")
        return self


class RewriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["standalone", "rewritten", "clarify"]
    standalone_query: str | None = Field(default=None, min_length=1, max_length=2000)
    history_scope: Literal["none", "recent", "clarification"] = "none"
    referenced_message_ids: list[UUID] = Field(default_factory=list, max_length=13)
    resolved_references: list[ResolvedReference] = Field(default_factory=list, max_length=4)
    clarification_reason: str | None = Field(default=None, min_length=1, max_length=500)
    clarification_options: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def consistent_structure(self):
        if any(not option.strip() or len(option) > 100 for option in self.clarification_options):
            raise ValueError("Invalid clarification option")
        if len(set(self.referenced_message_ids)) != len(self.referenced_message_ids):
            raise ValueError("Duplicate reference IDs")
        if self.decision == "clarify":
            if self.standalone_query is not None or not (self.clarification_reason or "").strip():
                raise ValueError("Clarification requires a reason and no retrieval query")
        elif not (self.standalone_query or "").strip() or self.clarification_reason is not None or self.clarification_options:
            raise ValueError("Retrieval decisions require a query and no clarification")
        return self


class RewritePolicyDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    artifact_type: Literal["rewrite_policy"] = "rewrite_policy"
    strategy: Literal["llm_v2"] = REWRITE_STRATEGY
    prompt_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RewriteArtifactDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    artifact_type: Literal["query_rewrite"] = "query_rewrite"
    strategy: Literal["llm_v2"] = REWRITE_STRATEGY
    prompt_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: RewriteResult
    # The actual bounded model INPUT, including for standalone results.
    history_message_ids: list[UUID] = Field(default_factory=list, max_length=12)
    input_pending_clarification_turn_id: UUID | None = None
    # Relationship selected by the model, not merely supplied in the input.
    pending_clarification_turn_id: UUID | None = None
    budget_basis: Literal["estimated_utf8_bytes_v1"] = "estimated_utf8_bytes_v1"
    input_bytes: int = Field(default=0, ge=0)
    input_estimated_tokens: int = Field(default=0, ge=0)
    validation_codes: list[Literal["json_structure", "bounded_output", "owned_references"]] = Field(default_factory=list, max_length=3)
    metrics: PersistenceMetrics = Field(default_factory=PersistenceMetrics)
