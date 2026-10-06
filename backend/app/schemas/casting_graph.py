"""Bounded stage-4 metadata. No prompts, messages or engineering payloads."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

CASTING_GRAPH_VERSION = "casting_v1_v3"
ROUTE_KEY = "casting_route:v1"
TOOL_KEY = "casting_tool:v1"


class CastingToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input_file_id: str = Field(min_length=36, max_length=36)

    @field_validator("input_file_id")
    @classmethod
    def canonical(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("Canonical UUID required")
        return value


class CastingRouteDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    artifact_type: Literal["casting_route"] = "casting_route"
    route: Literal["rag", "calculate", "input_required", "explain_existing", "clarify_selection"]
    effective_input_file_id: str | None
    source_run_id: str | None = None
    candidate_rank: int | None = Field(default=None, ge=1, le=16)
    tool_call_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    prompt_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("effective_input_file_id", "source_run_id")
    @classmethod
    def uuid(cls, value):
        if value is not None and str(UUID(value)) != value:
            raise ValueError("Canonical UUID required")
        return value

    @model_validator(mode="after")
    def linked(self):
        if (self.route == "calculate") != (self.tool_call_id is not None):
            raise ValueError("Calculation needs exactly one native tool call")
        if self.route == "calculate" and self.effective_input_file_id is None:
            raise ValueError("Calculation needs a frozen input")
        if (self.route == "explain_existing") != (self.source_run_id is not None):
            raise ValueError("Existing result route requires a run")
        if self.candidate_rank is not None and self.route != "explain_existing":
            raise ValueError("Candidate selection requires an existing result")
        return self


class CastingToolDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    artifact_type: Literal["casting_tool"] = "casting_tool"
    run_id: str
    input_file_id: str
    result_file_id: str | None
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: Literal["succeeded", "no_feasible_candidate", "admission_failed", "engine_failed", "timed_out", "interrupted"]
    projection_version: Literal["casting_summary_v1"] = "casting_summary_v1"
    projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,100}$")

    @field_validator("run_id", "input_file_id", "result_file_id")
    @classmethod
    def uuid(cls, value):
        return CastingRouteDetails.uuid(value)


CASTING_ARTIFACT_ADAPTER = TypeAdapter(Annotated[CastingRouteDetails | CastingToolDetails, Field(discriminator="artifact_type")])
