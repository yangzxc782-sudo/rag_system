"""Versioned reference-only state. No append reducers or process-local objects."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing_extensions import TypedDict
from app.schemas.query_rewrite import REWRITE_ARTIFACT_KEY


STATE_SCHEMA_VERSION = 1
GRAPH_VERSION = "phase13_m2_v1"
M3_GRAPH_VERSION = "phase13_m3_v2"
MAX_STATE_BYTES = 8192


class ConversationState(TypedDict):
    thread_id: str
    turn_id: str
    request_id: str
    attempt_no: int
    state_schema_version: int
    graph_version: str
    input_fingerprint: str
    current_message_id: str
    history_message_ids: list[str]
    pending_clarification_turn_id: str | None
    rewrite_artifact_id: str | None
    stage: str
    terminal_status: str | None
    outcome: str | None
    error_code: str | None


class StateContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    thread_id: str
    turn_id: str
    request_id: str
    attempt_no: int = Field(ge=1)
    state_schema_version: Literal[1] = STATE_SCHEMA_VERSION
    graph_version: Literal["phase13_m2_v1"] = GRAPH_VERSION
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_message_id: str
    history_message_ids: list[str] = Field(default_factory=list, max_length=12)
    pending_clarification_turn_id: str | None = None
    rewrite_artifact_id: str | None = None
    stage: Literal["accepted", "context_loaded", "understood", "ready_for_retrieval", "clarification_staged"] = "accepted"
    terminal_status: Literal["ready", "awaiting_clarification"] | None = None
    outcome: Literal["standalone", "rewritten", "clarification"] | None = None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,100}$")

    @field_validator("thread_id", "turn_id", "request_id", "current_message_id", "pending_clarification_turn_id", "rewrite_artifact_id")
    @classmethod
    def canonical_uuid(cls, value):
        if value is not None and str(UUID(value)) != value:
            raise ValueError("State IDs must be canonical UUIDs")
        return value

    @field_validator("history_message_ids")
    @classmethod
    def history_ids(cls, values):
        if len(set(values)) != len(values) or any(str(UUID(value)) != value for value in values):
            raise ValueError("Invalid bounded history IDs")
        return values


class ConversationStateV2(ConversationState):
    retrieval_artifact_id: str | None
    evidence_artifact_id: str | None
    generation_artifact_id: str | None
    result_artifact_id: str | None
    evidence_generation: int


class StateContractV2(StateContract):
    state_schema_version: Literal[2] = 2
    graph_version: Literal["phase13_m3_v2"] = M3_GRAPH_VERSION
    retrieval_artifact_id: str | None = None
    evidence_artifact_id: str | None = None
    generation_artifact_id: str | None = None
    result_artifact_id: str | None = None
    evidence_generation: int = Field(ge=1)
    stage: Literal["accepted", "context_loaded", "understood", "retrieved", "evidence_built",
                   "generated", "result_staged", "clarification_staged", "failed"] = "accepted"
    terminal_status: Literal["result_staged", "needs_recovery"] | None = None
    outcome: Literal["standalone", "rewritten", "answer", "no_context", "clarification"] | None = None

    @field_validator("retrieval_artifact_id", "evidence_artifact_id", "generation_artifact_id", "result_artifact_id")
    @classmethod
    def artifact_uuid(cls, value):
        return cls.canonical_uuid(value)


def validate_state(value: dict) -> ConversationState:
    contract = StateContractV2 if value.get("state_schema_version") == 2 else StateContract
    result = contract.model_validate(value).model_dump()
    if len(json.dumps(result).encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("Conversation state exceeds its fixed byte ceiling")
    return result


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    thread_id: UUID
    turn_id: UUID
    request_id: UUID
    attempt_no: int
    input_fingerprint: str

    @classmethod
    def from_state(cls, state: dict):
        return cls(UUID(state["thread_id"]), UUID(state["turn_id"]), UUID(state["request_id"]),
                   state["attempt_no"], state["input_fingerprint"])

    def validate(self, repo, *, lock: bool = False):
        from app.rag.query_rewrite_prompt import current_rewrite_policy
        turn = repo.validate_execution(self.thread_id, self.turn_id, self.request_id,
                                       self.attempt_no, self.input_fingerprint, lock=lock)
        repo.require_rewrite_policy(turn, current_rewrite_policy())
        return turn
