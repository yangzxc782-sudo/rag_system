"""Bounded ephemeral history. PostgreSQL messages remain the business truth."""
from __future__ import annotations

from dataclasses import dataclass
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.rag.conversation_state import REWRITE_ARTIFACT_KEY
from app.schemas.query_rewrite import RewriteArtifactDetails
from app.services.conversation_repository import ConversationError, ConversationRepository


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    id: UUID
    turn_id: UUID
    role: str
    content: str
    sequence_no: int


@dataclass(frozen=True, slots=True)
class PendingClarification:
    turn_id: UUID
    question: str
    message_id: UUID
    options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoryContext:
    messages: tuple[HistoryMessage, ...] = ()
    pending: PendingClarification | None = None
    budget_exhausted: bool = False

    @property
    def message_ids(self) -> list[UUID]:
        return [message.id for message in self.messages]


def estimated_tokens(text: str) -> int:
    """Conservative UTF-8-byte estimate, NOT the generation model's tokenizer."""
    return len(text.encode("utf-8"))


def message_cost(message: HistoryMessage) -> tuple[int, int]:
    # Include the ID/role envelope used by the rewrite prompt, not just text.
    payload = json.dumps({"message_id": str(message.id), "text": message.content}, ensure_ascii=False)
    size = len(payload.encode("utf-8"))
    return size, estimated_tokens(payload) + 32


def trim_history(messages: tuple[HistoryMessage, ...], settings: Settings,
                 *, pinned_turn_id: UUID | None = None) -> tuple[HistoryMessage, ...]:
    groups: dict[UUID, list[HistoryMessage]] = {}
    for message in sorted(messages, key=lambda item: item.sequence_no):
        groups.setdefault(message.turn_id, []).append(message)
    # Keep complete published pairs. A missing/oversized assistant must not leave
    # a user question masquerading as a completed round of discussion.
    selected = [pair for pair in groups.values() if {m.role for m in pair} == {"user", "assistant"} and len(pair) == 2]
    while selected:
        costs = [message_cost(m) for pair in selected for m in pair]
        if (len(selected) <= settings.conversation_history_max_turns
                and sum(c[0] for c in costs) <= settings.conversation_history_max_bytes
                and sum(c[1] for c in costs) <= settings.conversation_history_max_estimated_tokens):
            break
        index = next((i for i, pair in enumerate(selected) if pair[0].turn_id != pinned_turn_id), 0)
        selected.pop(index)
    return tuple(m for pair in selected for m in pair)


def _view(message) -> HistoryMessage:
    return HistoryMessage(message.id, message.turn_id, message.role, message.content, message.sequence_no)


def read_rewrite_details(artifact) -> RewriteArtifactDetails:
    if artifact.artifact_key != REWRITE_ARTIFACT_KEY or artifact.kind != "rewrite" or artifact.schema_version != 2:
        raise ConversationError("QA_REWRITE_STRATEGY_UNSUPPORTED", "Only LLM rewrite v2 can be executed.", status_code=409)
    try:
        return RewriteArtifactDetails.model_validate_json(json.dumps(artifact.details, ensure_ascii=False))
    except (ValidationError, TypeError, ValueError):
        raise ConversationError("QA_REWRITE_ARTIFACT_INVALID", "Stored rewrite metadata is invalid.", status_code=409) from None


class _PublishedClarificationLink(BaseModel):
    """Read-only projection of a completed legacy artifact, not an old rewriter."""
    model_config = ConfigDict(extra="forbid", strict=True)
    pending_clarification_turn_id: UUID | None = None
    options: list[str] = Field(default_factory=list, max_length=6)


def _clarification_link(repo, sid, latest):
    artifact = repo.get_artifact_by_key(sid, latest.id, attempt_no=latest.attempt_no, key=REWRITE_ARTIFACT_KEY)
    if artifact is not None:
        details = read_rewrite_details(artifact)
        if details.result.decision != "clarify":
            raise ConversationError("QA_CONTEXT_INVALID", "Published clarification metadata disagrees.", status_code=409)
        return details.pending_clarification_turn_id, tuple(details.result.clarification_options)
    artifact = repo.get_artifact_by_key(sid, latest.id, attempt_no=latest.attempt_no, key="query_rewrite:v1")
    if artifact is None or artifact.kind != "rewrite" or artifact.schema_version != 1:
        raise ConversationError("QA_CONTEXT_INVALID", "Published clarification metadata is missing.", status_code=409)
    try:
        data = artifact.details
        if data["result"]["decision"] != "clarify":
            raise ValueError("Not a clarification")
        link = _PublishedClarificationLink.model_validate_json(json.dumps({
            "pending_clarification_turn_id": data.get("pending_clarification_turn_id"),
            "options": data["result"].get("clarification_options", []),
        }))
        if any(not value.strip() or len(value) > 100 for value in link.options):
            raise ValueError("Invalid option")
        return link.pending_clarification_turn_id, tuple(link.options)
    except (ValidationError, TypeError, ValueError, KeyError):
        raise ConversationError("QA_CONTEXT_INVALID", "Invalid published clarification metadata.", status_code=409) from None


def select_history(repo: ConversationRepository, current_turn, settings: Settings) -> HistoryContext:
    sid = current_turn.session_id
    turns = repo.list_completed_turns(sid, before_turn_no=current_turn.turn_no,
                                     limit=settings.conversation_history_max_turns)
    newest_id = turns[0].id if turns else None
    pending = None
    pending_root = None
    if turns and turns[0].outcome == "clarification":
        latest = turns[0]
        root_id, options = _clarification_link(repo, sid, latest)
        root = repo.get_completed_turn(sid, root_id or latest.id, before_turn_no=current_turn.turn_no)
        if root.outcome != "clarification" or root.turn_no > latest.turn_no:
            raise ConversationError("QA_CONTEXT_INVALID", "Pending clarification is not a completed thread fact.", status_code=409)
        pending_root = (root.id, options)
        if all(turn.id != root.id for turn in turns):
            turns = turns[:settings.conversation_history_max_turns - 1] + [root]
    rows = repo.completed_messages(sid, [turn.id for turn in turns], before_turn_no=current_turn.turn_no,
                                  max_message_bytes=settings.conversation_history_max_bytes)
    if pending_root:
        user = next((m for m in rows if m.turn_id == pending_root[0] and m.role == "user"), None)
        if user is None:
            return HistoryContext(budget_exhausted=True)
        pending = PendingClarification(user.turn_id, user.content, user.id, pending_root[1])
    selected = trim_history(tuple(_view(row) for row in rows), settings,
                            pinned_turn_id=pending.turn_id if pending else None)
    # Never present older context as though the unavailable newest round existed.
    if newest_id and not any(m.turn_id == newest_id for m in selected):
        return HistoryContext(budget_exhausted=True)
    exhausted = bool(turns) and not selected
    if pending and pending.message_id not in {m.id for m in selected}:
        pending = None
        exhausted = True
    return HistoryContext(selected, pending, exhausted)


def rehydrate_history(repo: ConversationRepository, turn, ids: list[UUID],
                      pending_turn_id: UUID | None, settings: Settings) -> HistoryContext:
    rows = repo.history_messages_by_ids(turn.session_id, ids, before_turn_no=turn.turn_no,
                                       max_message_bytes=settings.conversation_history_max_bytes)
    messages = tuple(_view(row) for row in rows)
    if trim_history(messages, settings, pinned_turn_id=pending_turn_id) != messages:
        raise ConversationError("QA_CONTEXT_INVALID", "Saved history exceeds the current budget.", status_code=409)
    pending = None
    if pending_turn_id:
        current = select_history(repo, turn, settings)
        if current.pending is None or current.pending.turn_id != pending_turn_id:
            raise ConversationError("QA_CONTEXT_INVALID", "Pending clarification reference is stale.", status_code=409)
        pending = current.pending
        if pending.message_id not in ids:
            raise ConversationError("QA_CONTEXT_INVALID", "Pending question is absent from bounded history.", status_code=409)
    return HistoryContext(messages, pending)
