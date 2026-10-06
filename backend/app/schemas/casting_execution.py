"""Validate the frozen engine's result shape without changing any stored value."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CastingCandidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    id: str
    rank: int = Field(ge=1)
    strategy: Literal["local-min", "uniform", "local-plus-one", "uniform-plus-one"]
    site_ids: list[str] = Field(min_length=1, max_length=24)
    risers: list[dict[str, Any]] = Field(min_length=1, max_length=24)
    riser_count: int = Field(ge=1, le=24)
    gating: dict[str, Any]
    gross_pour_mass_kg: float
    riser_metal_mass_kg: float
    gating_metal_estimate_kg: float
    yield_percent: float
    sand_metal_ratio: float
    checks: dict[str, bool]
    preliminary_feasible: bool
    trace: dict[str, Any]
    ontology_status: Literal["NeedsReview"]
    real_cae: Literal["Pending"]
    used_input_snapshot: str
    used_rule_version: str
    pending_evidence: list[str]
    model_ref: str
    explanations: list[dict[str, Any]]


class CastingRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    case_id: str
    snapshot_id: str
    rule_set: str
    rule_version: str
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    rules_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_facts: dict[str, Any]
    input_digest: dict[str, Any]
    ranking_basis: str
    recommended_candidate_id: str | None
    candidates: list[CastingCandidateResult]
    rejected_attempts: list[dict[str, Any]]
    generation_boundary: str
    admission: dict[str, Any]
    run_id: str
    ontology_triples: int = Field(gt=0)
