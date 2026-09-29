"""Caller-owned short transactions for Phase 13 business persistence.

Use ``with session.begin():`` for each mutation. No method commits, rolls back,
opens a connection, invokes a model, or owns a long-lived execution lock.
Source-bearing operations must precede any other QA-locking operation in their
transaction; batch snapshots lock all Documents before any session/turn row.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, load_only

from app.core.errors import BusinessError
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.qa_evidence_snapshot import QAEvidenceSnapshot
from app.models.qa_evidence_source import QAEvidenceSource
from app.models.qa_message import QAMessage
from app.models.qa_session import QASession
from app.models.qa_turn import QATurn
from app.models.qa_turn_artifact import QATurnArtifact
from app.models.retrieval_log import RetrievalLog
from app.schemas.conversation_persistence import (
    AnswerDraft, EvidenceSnapshotView, EvidenceSourceRef, PersistenceMetrics, SnapshotInput,
)
from app.services.document_operation_guard import DocumentOperationGuard
from app.schemas.query_rewrite import (
    REWRITE_ARTIFACT_KEY, REWRITE_POLICY_KEY, RewriteArtifactDetails, RewritePolicyDetails,
)
from app.schemas.conversation_rag import StageDetails, STAGE_ADAPTER, STAGE_KINDS


_ACTIVE = ("running", "finalizing", "needs_recovery")
_STAGES = {"context", "rewrite", "retrieval", "evidence", "generation", "result"}
_TRANSITIONS = {
    "running": {"finalizing", "failed", "needs_recovery"},
    "finalizing": {"failed", "needs_recovery"},
    "failed": {"running"},
    "needs_recovery": {"running", "failed"},
    "completed": set(),
}
_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_LOCK_MARKER = "phase13_qa_lock_transaction"


class ConversationError(BusinessError):
    pass


def _error(code: str, message: str, status: int = 409) -> ConversationError:
    return ConversationError(code, message, status_code=status)


def fingerprint(value: Any) -> str:
    """Canonical JSON identity; rejects non-JSON and non-finite numbers."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _metrics(value: PersistenceMetrics | dict[str, Any] | None) -> dict[str, Any]:
    try:
        model = PersistenceMetrics.model_validate(value or {})
    except ValidationError as exc:
        raise _error("QA_METADATA_INVALID", "Only typed persistence metrics are allowed.", 422) from exc
    return model.model_dump(mode="json", exclude_none=True)


def _key(value: str) -> None:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise _error("QA_KEY_INVALID", "Invalid persistence key.", 422)


def _sources(refs: tuple[EvidenceSourceRef, ...]) -> tuple[EvidenceSourceRef, ...]:
    for ref in refs:
        if not isinstance(ref, EvidenceSourceRef) or not isinstance(ref.document_id, UUID):
            raise _error("QA_SOURCE_INVALID", "Source document must be a UUID.", 422)
        if ref.chunk_id is not None and not isinstance(ref.chunk_id, UUID):
            raise _error("QA_SOURCE_INVALID", "Source chunk must be a UUID.", 422)
    return tuple(sorted(set(refs), key=lambda r: (str(r.document_id), str(r.chunk_id or ""))))


class ConversationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _transaction(self) -> None:
        if not self.db.in_transaction():
            raise _error("QA_TRANSACTION_REQUIRED", "Use a caller-owned short transaction.", 500)

    def _mark_lock(self) -> None:
        self._transaction()
        self.db.info[_LOCK_MARKER] = self.db.get_transaction()

    def _guard_sources(self, refs: tuple[EvidenceSourceRef, ...]) -> None:
        self._transaction()
        if refs and self.db.info.get(_LOCK_MARKER) is self.db.get_transaction():
            raise _error("QA_LOCK_ORDER", "Source-bearing writes need a fresh Document-first transaction.", 500)
        DocumentOperationGuard(self.db).lock_normal_many({r.document_id for r in refs})
        chunk_ids = {r.chunk_id for r in refs if r.chunk_id is not None}
        if chunk_ids:
            owners = dict(self.db.execute(
                select(DocumentChunk.id, DocumentChunk.document_id).where(DocumentChunk.id.in_(chunk_ids))
            ).all())
            if any(r.chunk_id is not None and owners.get(r.chunk_id) != r.document_id for r in refs):
                raise _error("QA_SOURCE_INVALID", "Chunk is missing or belongs to another document.")

    def get_session(self, session_id: UUID, *, for_update: bool = False) -> QASession:
        statement = select(QASession).where(QASession.id == session_id).execution_options(populate_existing=True)
        if for_update:
            self._mark_lock()
            statement = statement.with_for_update()
        row = self.db.scalar(statement)
        if row is None:
            raise _error("QA_SESSION_NOT_FOUND", "Session not found.", 404)
        return row

    def list_sessions(self, *, limit: int = 50, offset: int = 0, before=None) -> list[QASession]:
        self._page(limit, offset)
        statement = select(QASession)
        if before is not None:
            statement = statement.where(tuple_(QASession.updated_at, QASession.id) < tuple_(*before))
        return list(self.db.scalars(statement.order_by(
            QASession.updated_at.desc(), QASession.id.desc(),
        ).limit(limit).offset(offset)).all())

    def rename_session(self, session_id: UUID, title: str) -> QASession:
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 255:
            raise _error("QA_TITLE_INVALID", "Invalid session title.", 422)
        row = self.get_session(session_id, for_update=True)
        row.title, row.updated_at = title.strip(), func.now()
        self.db.flush()
        return row

    def active_turn(self, session_id: UUID) -> QATurn | None:
        self.get_session(session_id)
        return self.db.scalar(select(QATurn).where(
            QATurn.session_id == session_id, QATurn.status.in_(_ACTIVE)))

    def has_later_turn(self, session_id: UUID, turn_no: int) -> bool:
        return self.db.scalar(select(QATurn.id).where(
            QATurn.session_id == session_id, QATurn.turn_no > turn_no).limit(1)) is not None

    def restore_interrupted_turn(self, session_id: UUID, turn_id: UUID, *, expected_attempt: int) -> QATurn:
        """M4 only: under the execution lock, resume a non-failed same attempt.

        The coordinator must first reconcile the checkpoint. A terminal graph
        failure uses transition_turn instead and increments the attempt.
        """
        session, turn = self._locked_turn(session_id, turn_id, expected_attempt)
        if (turn.status not in {"needs_recovery", "finalizing"}
                or self.has_later_turn(session_id, turn.turn_no)
                or turn.error_code not in {None, "QA_EXECUTION_INTERRUPTED", "QA_PUBLICATION_INTERRUPTED"}):
            raise _error("QA_RECOVERY_CONFLICT", "This turn cannot resume the same attempt.")
        turn.status, turn.error_code = "running", None
        session.updated_at = turn.updated_at = func.now()
        self.db.flush()
        return turn

    def create_session(self, request_id: UUID, *, title: str | None = None) -> QASession:
        self._transaction()
        if not isinstance(request_id, UUID):
            raise _error("QA_REQUEST_INVALID", "Creation request_id must be a UUID.", 422)
        if title is not None:
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 255:
                raise _error("QA_TITLE_INVALID", "Title must contain 1 to 255 characters.", 422)
            title = title.strip()
        identity = fingerprint({"title": title})
        self.db.execute(insert(QASession).values(
            id=uuid4(), create_request_id=request_id, create_fingerprint=identity, title=title,
        ).on_conflict_do_nothing(constraint="uq_qa_sessions_create_request"))
        row = self.db.scalar(select(QASession).where(QASession.create_request_id == request_id))
        if row is None or row.create_fingerprint != identity:
            raise _error("QA_REQUEST_CONFLICT", "Creation request parameters conflict.")
        return row

    def get_turn(self, session_id: UUID, turn_id: UUID) -> QATurn:
        row = self.db.scalar(select(QATurn).where(
            QATurn.id == turn_id, QATurn.session_id == session_id,
        ).execution_options(populate_existing=True))
        if row is None:
            raise _error("QA_TURN_NOT_FOUND", "Turn not found in this session.", 404)
        return row

    def get_turn_by_request(self, session_id: UUID, request_id: UUID) -> QATurn | None:
        return self.db.scalar(select(QATurn).where(
            QATurn.session_id == session_id, QATurn.request_id == request_id,
        ).execution_options(populate_existing=True))

    def _locked_turn(self, session_id: UUID, turn_id: UUID, attempt_no: int) -> tuple[QASession, QATurn]:
        session = self.get_session(session_id, for_update=True)
        turn = self.db.scalar(select(QATurn).where(
            QATurn.session_id == session_id, QATurn.id == turn_id,
        ).execution_options(populate_existing=True).with_for_update())
        if turn is None:
            raise _error("QA_TURN_NOT_FOUND", "Turn not found in this session.", 404)
        if turn.attempt_no != attempt_no:
            raise _error("QA_ATTEMPT_CONFLICT", "Execution attempt is stale.")
        return session, turn

    def start_turn(
        self, session_id: UUID, request_id: UUID, question: str, *,
        limit: int = 8, document_id: UUID | None = None,
        rewrite_policy: RewritePolicyDetails | None = None,
    ) -> QATurn:
        self._transaction()
        if (not isinstance(request_id, UUID) or not isinstance(question, str) or not question.strip()
                or type(limit) is not int or not 1 <= limit <= 50
                or (document_id is not None and not isinstance(document_id, UUID))):
            raise _error("QA_REQUEST_INVALID", "Invalid turn parameters.", 422)
        identity = fingerprint({"question": question, "limit": limit, "document_id": str(document_id) if document_id else None})
        session = self.get_session(session_id, for_update=True)
        existing = self.get_turn_by_request(session_id, request_id)
        if existing is not None:
            if existing.request_fingerprint != identity:
                raise _error("QA_REQUEST_CONFLICT", "Request parameters conflict with the saved turn.")
            return existing
        if self.db.scalar(select(QATurn.id).where(QATurn.session_id == session_id, QATurn.status.in_(_ACTIVE)).limit(1)):
            raise _error("QA_THREAD_BUSY", "Resolve the active turn before creating another.")
        turn = QATurn(
            id=uuid4(), session_id=session_id, request_id=request_id, request_fingerprint=identity,
            turn_no=session.next_turn_no, question=question, retrieval_limit=limit, document_id=document_id,
            status="running", attempt_no=1,
        )
        self.db.add(turn)
        self.db.flush()
        self.db.add(QAMessage(
            id=uuid4(), session_id=session_id, turn_id=turn.id, sequence_no=session.next_message_seq,
            role="user", content=question,
        ))
        session.next_turn_no += 1
        session.next_message_seq += 1
        session.updated_at = func.now()
        self.db.flush()
        if rewrite_policy is not None:
            self.save_rewrite_policy(turn, rewrite_policy)
        return turn

    def list_messages(self, session_id: UUID, *, limit: int = 50, before_seq: int | None = None) -> list[QAMessage]:
        self._page(limit, 0)
        self.get_session(session_id)
        statement = select(QAMessage).where(QAMessage.session_id == session_id)
        if before_seq is not None:
            if type(before_seq) is not int or before_seq <= 0:
                raise _error("QA_PAGE_INVALID", "Invalid message cursor.", 422)
            statement = statement.where(QAMessage.sequence_no < before_seq)
        rows = self.db.scalars(statement.order_by(QAMessage.sequence_no.desc()).limit(limit)).all()
        return list(reversed(rows))

    @staticmethod
    def _page(limit: int, offset: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise _error("QA_PAGE_INVALID", "Invalid page size or offset.", 422)

    def transition_turn(
        self, session_id: UUID, turn_id: UUID, *, expected_status: str,
        new_status: str, expected_attempt: int, error_code: str | None = None,
        rewrite_policy: RewritePolicyDetails | None = None,
    ) -> QATurn:
        session, turn = self._locked_turn(session_id, turn_id, expected_attempt)
        if turn.status != expected_status or new_status not in _TRANSITIONS.get(turn.status, set()):
            raise _error("QA_STATE_CONFLICT", "Invalid or stale turn transition.")
        if new_status in {"failed", "needs_recovery"}:
            if not isinstance(error_code, str) or not _KEY.fullmatch(error_code):
                raise _error("QA_ERROR_INVALID", "A safe error code is required.", 422)
        elif error_code is not None:
            raise _error("QA_ERROR_INVALID", "A running turn cannot carry a failure code.", 422)
        if new_status == "running":
            if rewrite_policy is not None:
                self.require_rewrite_policy(turn, rewrite_policy)
            later = self.db.scalar(select(QATurn.id).where(
                QATurn.session_id == session_id, QATurn.turn_no > turn.turn_no,
            ).limit(1))
            if later is not None:
                raise _error("QA_TURN_SUPERSEDED", "A later turn has already been accepted.")
            turn.attempt_no += 1
        turn.status, turn.error_code = new_status, error_code
        turn.updated_at = session.updated_at = func.now()
        self.db.flush()
        if new_status == "running" and rewrite_policy is not None:
            self.save_rewrite_policy(turn, rewrite_policy)
        return turn

    def get_artifact(self, session_id: UUID, turn_id: UUID, artifact_id: UUID) -> QATurnArtifact:
        row = self.db.scalar(select(QATurnArtifact).where(
            QATurnArtifact.session_id == session_id, QATurnArtifact.turn_id == turn_id,
            QATurnArtifact.id == artifact_id,
        ).execution_options(populate_existing=True))
        if row is None:
            raise _error("QA_ARTIFACT_NOT_FOUND", "Artifact not found in this turn.", 404)
        return row

    def get_artifact_by_key(
        self, session_id: UUID, turn_id: UUID, *, attempt_no: int, key: str,
    ) -> QATurnArtifact | None:
        """Find a persisted stage without relying on process-local artifact IDs."""
        _key(key)
        if type(attempt_no) is not int or attempt_no < 1:
            raise _error("QA_ATTEMPT_INVALID", "Attempt number must be positive.", 422)
        self.get_turn(session_id, turn_id)
        return self.db.scalar(select(QATurnArtifact).where(
            QATurnArtifact.session_id == session_id, QATurnArtifact.turn_id == turn_id,
            QATurnArtifact.attempt_no == attempt_no, QATurnArtifact.artifact_key == key,
        ).execution_options(populate_existing=True))

    def save_artifact(
        self, session_id: UUID, turn_id: UUID, *, expected_attempt: int, key: str,
        kind: str, input_fingerprint: str,
        details: PersistenceMetrics | RewriteArtifactDetails | RewritePolicyDetails | StageDetails | dict[str, Any] | None = None,
        parent_artifact_id: UUID | None = None, schema_version: int = 1,
    ) -> QATurnArtifact:
        _key(key)
        if (kind not in _STAGES or not isinstance(input_fingerprint, str)
                or not _HASH.fullmatch(input_fingerprint) or type(schema_version) is not int or schema_version < 1):
            raise _error("QA_ARTIFACT_INVALID", "Invalid stage, fingerprint or schema version.", 422)
        if isinstance(details, RewritePolicyDetails) or isinstance(details, dict) and details.get("artifact_type") == "rewrite_policy":
            try:
                parsed = (details if isinstance(details, RewritePolicyDetails)
                          else RewritePolicyDetails.model_validate_json(json.dumps(details)))
                if (kind, key, schema_version, parent_artifact_id) != ("context", REWRITE_POLICY_KEY, 2, None):
                    raise ValueError("Policy identity mismatch")
                metrics = parsed.model_dump(mode="json")
            except (ValidationError, TypeError, ValueError) as exc:
                raise _error("QA_METADATA_INVALID", "Invalid rewrite policy artifact.", 422) from exc
        elif kind == "rewrite" and (
            isinstance(details, RewriteArtifactDetails)
            or isinstance(details, dict) and details.get("artifact_type") == "query_rewrite"
        ):
            try:
                parsed = (details if isinstance(details, RewriteArtifactDetails)
                          else RewriteArtifactDetails.model_validate_json(json.dumps(details)))
                if (key, schema_version, parent_artifact_id) != (REWRITE_ARTIFACT_KEY, 2, None):
                    raise ValueError("Rewrite version mismatch")
                metrics = parsed.model_dump(mode="json", exclude_none=True)
            except (ValidationError, TypeError, ValueError) as exc:
                raise _error("QA_METADATA_INVALID", "Invalid typed rewrite artifact.", 422) from exc
        elif getattr(details, "artifact_type", None) in STAGE_KINDS or (
            isinstance(details, dict) and details.get("artifact_type") in STAGE_KINDS
        ):
            try:
                value = details.model_dump(mode="json") if hasattr(details, "model_dump") else details
                parsed = STAGE_ADAPTER.validate_json(json.dumps(value))
                if STAGE_KINDS[parsed.artifact_type] != kind:
                    raise ValueError("Stage kind mismatch")
                metrics = parsed.model_dump(mode="json", exclude_none=True)
            except (ValidationError, TypeError, ValueError) as exc:
                raise _error("QA_METADATA_INVALID", "Invalid typed chat stage artifact.", 422) from exc
        else:
            metrics = _metrics(details)
        _, turn = self._locked_turn(session_id, turn_id, expected_attempt)
        if turn.status != "running":
            raise _error("QA_STATE_CONFLICT", "Artifacts can only be saved for a running turn.")
        if parent_artifact_id is not None:
            self.get_artifact(session_id, turn_id, parent_artifact_id)
        identity = fingerprint({"kind": kind, "input": input_fingerprint, "details": metrics,
                                "parent": str(parent_artifact_id) if parent_artifact_id else None, "version": schema_version})
        existing = self.db.scalar(select(QATurnArtifact).where(
            QATurnArtifact.turn_id == turn_id, QATurnArtifact.attempt_no == expected_attempt,
            QATurnArtifact.artifact_key == key,
        ))
        if existing is not None:
            if existing.content_fingerprint != identity:
                raise _error("QA_ARTIFACT_CONFLICT", "Stage output conflicts with its immutable artifact.")
            return existing
        row = QATurnArtifact(
            id=uuid4(), session_id=session_id, turn_id=turn_id, attempt_no=expected_attempt,
            artifact_key=key, kind=kind, schema_version=schema_version, input_fingerprint=input_fingerprint,
            content_fingerprint=identity, parent_artifact_id=parent_artifact_id, details=metrics,
        )
        self.db.add(row)
        self.db.flush()
        return row

    @staticmethod
    def _policy_fingerprint(turn, policy: RewritePolicyDetails) -> str:
        return fingerprint({"thread": str(turn.session_id), "turn": str(turn.id),
            "request_id": str(turn.request_id), "request": turn.request_fingerprint,
            "attempt": turn.attempt_no, "policy": policy.model_dump(mode="json")})

    def save_rewrite_policy(self, turn: QATurn, policy: RewritePolicyDetails) -> QATurnArtifact:
        """Only at new turn/attempt acceptance, in the caller's same transaction."""
        return self.save_artifact(turn.session_id, turn.id, expected_attempt=turn.attempt_no,
            key=REWRITE_POLICY_KEY, kind="context", schema_version=2, details=policy,
            input_fingerprint=self._policy_fingerprint(turn, policy))

    def require_rewrite_policy(self, turn: QATurn, policy: RewritePolicyDetails) -> None:
        row = self.get_artifact_by_key(turn.session_id, turn.id,
                                      attempt_no=turn.attempt_no, key=REWRITE_POLICY_KEY)
        if (row is None or row.kind != "context" or row.schema_version != 2 or row.parent_artifact_id is not None
                or row.details != policy.model_dump(mode="json")
                or row.input_fingerprint != self._policy_fingerprint(turn, policy)):
            raise _error("QA_REWRITE_STRATEGY_UNSUPPORTED", "This execution does not belong to the current LLM rewrite policy.")
        if self.get_artifact_by_key(turn.session_id, turn.id, attempt_no=turn.attempt_no, key="query_rewrite:v1"):
            raise _error("QA_REWRITE_STRATEGY_UNSUPPORTED", "Legacy rewrite executions cannot resume.")

    def validate_execution(
        self, session_id: UUID, turn_id: UUID, request_id: UUID, attempt_no: int,
        input_fingerprint: str, *, lock: bool = False,
    ) -> QATurn:
        """Short write fence; never hold this transaction across a model call."""
        turn = self._locked_turn(session_id, turn_id, attempt_no)[1] if lock else self.get_turn(session_id, turn_id)
        if (turn.request_id != request_id or turn.attempt_no != attempt_no
                or turn.request_fingerprint != input_fingerprint or turn.status != "running"):
            raise _error("QA_EXECUTION_STALE", "Execution identity or business state is stale.")
        if self.db.scalar(select(QATurn.id).where(
            QATurn.session_id == session_id, QATurn.turn_no > turn.turn_no,
        ).limit(1)) is not None:
            raise _error("QA_EXECUTION_STALE", "A later business turn exists.")
        return turn

    def get_user_message(self, session_id: UUID, turn_id: UUID) -> QAMessage:
        self.get_turn(session_id, turn_id)
        row = self.db.scalar(select(QAMessage).where(
            QAMessage.session_id == session_id, QAMessage.turn_id == turn_id, QAMessage.role == "user",
        ))
        if row is None:
            raise _error("QA_MESSAGE_NOT_FOUND", "User message not found in this turn.", 404)
        return row

    def list_completed_turns(self, session_id: UUID, *, before_turn_no: int, limit: int = 6) -> list[QATurn]:
        self._page(limit, 0)
        self.get_session(session_id)
        return list(self.db.scalars(select(QATurn).options(self._history_turn_columns()).where(
            QATurn.session_id == session_id, QATurn.status == "completed", QATurn.turn_no < before_turn_no,
        ).order_by(QATurn.turn_no.desc()).limit(limit)).all())

    @staticmethod
    def _history_turn_columns():
        # Selecting history identities must not materialize unbounded old question
        # text. Message bodies are fetched separately with an octet_length guard.
        return load_only(QATurn.id, QATurn.session_id, QATurn.turn_no, QATurn.status,
                         QATurn.outcome, QATurn.attempt_no, raiseload=True)

    def get_completed_turn(self, session_id: UUID, turn_id: UUID, *, before_turn_no: int) -> QATurn:
        row = self.db.scalar(select(QATurn).options(self._history_turn_columns()).where(
            QATurn.session_id == session_id, QATurn.id == turn_id,
            QATurn.status == "completed", QATurn.turn_no < before_turn_no,
        ))
        if row is None:
            raise _error("QA_CONTEXT_INVALID", "Completed history turn is unavailable in this thread.")
        return row

    def completed_messages(
        self, session_id: UUID, turn_ids: list[UUID], *, before_turn_no: int, max_message_bytes: int,
    ) -> list[QAMessage]:
        if len(turn_ids) > 6 or max_message_bytes <= 0:
            raise _error("QA_CONTEXT_INVALID", "Invalid bounded history selection.", 422)
        return list(self.db.scalars(select(QAMessage).join(
            QATurn, (QATurn.id == QAMessage.turn_id) & (QATurn.session_id == QAMessage.session_id),
        ).where(
            QAMessage.session_id == session_id, QATurn.session_id == session_id,
            QATurn.id.in_(turn_ids), QATurn.status == "completed", QATurn.turn_no < before_turn_no,
            func.octet_length(QAMessage.content) <= max_message_bytes,
        ).order_by(QAMessage.sequence_no)).all())

    def history_messages_by_ids(
        self, session_id: UUID, message_ids: list[UUID], *, before_turn_no: int, max_message_bytes: int,
    ) -> list[QAMessage]:
        """Always scope IDs to a thread and completed turns before reading text."""
        if len(message_ids) > 12 or len(set(message_ids)) != len(message_ids):
            raise _error("QA_CONTEXT_INVALID", "Invalid history reference set.", 422)
        rows = list(self.db.scalars(select(QAMessage).join(
            QATurn, (QATurn.id == QAMessage.turn_id) & (QATurn.session_id == QAMessage.session_id),
        ).where(
            QAMessage.session_id == session_id, QATurn.session_id == session_id,
            QAMessage.id.in_(message_ids), QATurn.status == "completed", QATurn.turn_no < before_turn_no,
            func.octet_length(QAMessage.content) <= max_message_bytes,
        ).order_by(QAMessage.sequence_no)).all())
        if {row.id for row in rows} != set(message_ids):
            raise _error("QA_CONTEXT_INVALID", "History references are unavailable in this thread.")
        return rows

    def save_snapshots(
        self, session_id: UUID, turn_id: UUID, artifact_id: UUID, *,
        expected_attempt: int, snapshots: tuple[SnapshotInput, ...],
    ) -> tuple[EvidenceSnapshotView, ...]:
        prepared = self._prepare_snapshots(snapshots)
        self._guard_sources(_sources(tuple(r for _, refs, _ in prepared for r in refs)))
        return self._write_snapshots(session_id, turn_id, artifact_id, expected_attempt, prepared)

    @staticmethod
    def _prepare_snapshots(snapshots: tuple[SnapshotInput, ...]) -> list:
        prepared = []
        for item in snapshots:
            _key(item.key)
            refs = _sources(item.sources)
            if item.kind not in {"candidate", "citation", "graph", "answer_draft"} or type(item.schema_version) is not int or item.schema_version < 1:
                raise _error("QA_SNAPSHOT_INVALID", "Invalid evidence kind or schema version.", 422)
            if not isinstance(item.payload, dict):
                raise _error("QA_SNAPSHOT_INVALID", "Evidence payload must be an object.", 422)
            if item.kind == "answer_draft":
                try:
                    draft = AnswerDraft.model_validate(item.payload)
                except ValidationError as exc:
                    raise _error("QA_DRAFT_INVALID", "Invalid answer draft.", 422) from exc
                if not draft.text.strip() or (draft.outcome == "answer") != bool(refs):
                    raise _error("QA_SOURCE_REQUIRED", "Answer drafts need sources; clarification/no-context drafts must be source-free.", 422)
            elif not refs:
                raise _error("QA_SOURCE_REQUIRED", "Evidence requires normalized sources.", 422)
            try:
                identity = fingerprint({"kind": item.kind, "version": item.schema_version, "payload": item.payload,
                                        "sources": [[str(r.document_id), str(r.chunk_id) if r.chunk_id else None] for r in refs]})
            except (TypeError, ValueError) as exc:
                raise _error("QA_SNAPSHOT_INVALID", "Evidence payload must be finite JSON.", 422) from exc
            prepared.append((item, refs, identity))
        if not prepared or len({item.key for item, _, _ in prepared}) != len(prepared):
            raise _error("QA_SNAPSHOT_INVALID", "Supply a non-empty batch of unique snapshot keys.", 422)
        return prepared

    def save_stage(
        self, session_id: UUID, turn_id: UUID, *, expected_attempt: int, key: str,
        kind: str, input_fingerprint: str, details: StageDetails,
        snapshots: tuple[SnapshotInput, ...] = (), parent_artifact_id: UUID | None = None,
    ) -> QATurnArtifact:
        """Atomically commit stage + snapshots, Document before QA locks.

        Caller still owns the transaction. This composes the same validation and
        write code as save_artifact/save_snapshots; it never bypasses source guards.
        """
        prepared = self._prepare_snapshots(snapshots) if snapshots else []
        self._guard_sources(_sources(tuple(r for _, refs, _ in prepared for r in refs)))
        artifact = self.save_artifact(session_id, turn_id, expected_attempt=expected_attempt,
            key=key, kind=kind, input_fingerprint=input_fingerprint, details=details,
            parent_artifact_id=parent_artifact_id)
        if prepared:
            self._write_snapshots(session_id, turn_id, artifact.id, expected_attempt, prepared)
        return artifact

    def _write_snapshots(self, session_id, turn_id, artifact_id, expected_attempt, prepared):
        # Private: both entry points validated and locked every source FIRST.
        _, turn = self._locked_turn(session_id, turn_id, expected_attempt)
        if turn.status != "running":
            raise _error("QA_STATE_CONFLICT", "Evidence can only be saved for a running turn.")
        artifact = self.get_artifact(session_id, turn_id, artifact_id)
        if artifact.attempt_no != expected_attempt:
            raise _error("QA_ATTEMPT_CONFLICT", "Cannot append evidence to an old attempt.")
        ids = []
        for item, refs, identity in prepared:
            row = self.db.scalar(select(QAEvidenceSnapshot).where(
                QAEvidenceSnapshot.artifact_id == artifact_id, QAEvidenceSnapshot.snapshot_key == item.key,
            ).execution_options(populate_existing=True))
            if row is not None:
                if row.content_fingerprint != identity:
                    raise _error("QA_SNAPSHOT_CONFLICT", "Evidence conflicts with its immutable snapshot.")
                if row.status != "available":
                    raise _error("QA_EVIDENCE_UNAVAILABLE", "Deleted evidence cannot be restored.")
            else:
                row = QAEvidenceSnapshot(
                    id=uuid4(), session_id=session_id, turn_id=turn_id, artifact_id=artifact_id,
                    snapshot_key=item.key, kind=item.kind, schema_version=item.schema_version,
                    payload=deepcopy(item.payload), content_fingerprint=identity, status="available",
                )
                self.db.add(row)
                self.db.flush()
                self.db.add_all([QAEvidenceSource(
                    id=uuid4(), session_id=session_id, turn_id=turn_id, snapshot_id=row.id,
                    document_id=r.document_id, chunk_id=r.chunk_id, status="available",
                ) for r in refs])
            ids.append(row.id)
        self.db.flush()
        return tuple(self.get_snapshot(session_id, turn_id, ident) for ident in ids)

    def _snapshot(self, session_id: UUID, turn_id: UUID, snapshot_id: UUID, *, lock: bool = False) -> QAEvidenceSnapshot:
        statement = select(QAEvidenceSnapshot).where(
            QAEvidenceSnapshot.session_id == session_id, QAEvidenceSnapshot.turn_id == turn_id,
            QAEvidenceSnapshot.id == snapshot_id,
        ).execution_options(populate_existing=True)
        row = self.db.scalar(statement.with_for_update() if lock else statement)
        if row is None:
            raise _error("QA_SNAPSHOT_NOT_FOUND", "Evidence not found in this turn.", 404)
        return row

    def get_snapshot(self, session_id: UUID, turn_id: UUID, snapshot_id: UUID) -> EvidenceSnapshotView:
        row = self._snapshot(session_id, turn_id, snapshot_id)
        sources = self.db.execute(select(
            QAEvidenceSource.document_id, QAEvidenceSource.chunk_id, QAEvidenceSource.status,
            Document.deletion_status,
        ).outerjoin(Document, Document.id == QAEvidenceSource.document_id).where(
            QAEvidenceSource.snapshot_id == snapshot_id, QAEvidenceSource.session_id == session_id,
            QAEvidenceSource.turn_id == turn_id,
        )).all()
        refs = _sources(tuple(EvidenceSourceRef(s.document_id, s.chunk_id) for s in sources))
        available = row.status == "available" and all(s.status == "available" and s.deletion_status == "normal" for s in sources)
        if not refs and row.kind != "answer_draft":
            available = False
        return EvidenceSnapshotView(
            id=row.id, session_id=row.session_id, turn_id=row.turn_id, artifact_id=row.artifact_id,
            kind=row.kind, status=row.status if available or row.status == "source_deleted" else "source_unavailable",
            payload=deepcopy(row.payload) if available else None, sources=refs, redacted_at=row.redacted_at,
        )

    def get_snapshot_by_key(self, session_id: UUID, turn_id: UUID, artifact_id: UUID, key: str) -> EvidenceSnapshotView:
        _key(key)
        ident = self.db.scalar(select(QAEvidenceSnapshot.id).where(
            QAEvidenceSnapshot.session_id == session_id, QAEvidenceSnapshot.turn_id == turn_id,
            QAEvidenceSnapshot.artifact_id == artifact_id, QAEvidenceSnapshot.snapshot_key == key,
        ))
        if ident is None:
            raise _error("QA_SNAPSHOT_NOT_FOUND", "Stage snapshot is missing.", 404)
        return self.get_snapshot(session_id, turn_id, ident)

    def get_answer(self, session_id: UUID, turn_id: UUID) -> QAMessage | None:
        self.get_turn(session_id, turn_id)
        return self.db.scalar(select(QAMessage).where(
            QAMessage.session_id == session_id, QAMessage.turn_id == turn_id, QAMessage.role == "assistant",
        ))

    def list_snapshots(
        self, session_id: UUID, turn_id: UUID, artifact_id: UUID, *,
        limit: int = 50, offset: int = 0,
    ) -> list[EvidenceSnapshotView]:
        """Return paged, visibility-checked evidence, never raw stored payloads."""
        self._page(limit, offset)
        self.get_artifact(session_id, turn_id, artifact_id)
        ids = self.db.scalars(select(QAEvidenceSnapshot.id).where(
            QAEvidenceSnapshot.session_id == session_id, QAEvidenceSnapshot.turn_id == turn_id,
            QAEvidenceSnapshot.artifact_id == artifact_id,
        ).order_by(QAEvidenceSnapshot.snapshot_key, QAEvidenceSnapshot.id).limit(limit).offset(offset)).all()
        return [self.get_snapshot(session_id, turn_id, ident) for ident in ids]

    def publish_answer(
        self, session_id: UUID, turn_id: UUID, snapshot_id: UUID, *, expected_attempt: int,
        finalize: bool = False,
    ) -> QAMessage:
        self._transaction()
        existing = self.get_answer(session_id, turn_id)
        if existing is not None:
            if existing.answer_snapshot_id != snapshot_id:
                raise _error("QA_ANSWER_CONFLICT", "A different answer is already published.")
            return existing
        view = self.get_snapshot(session_id, turn_id, snapshot_id)
        if view.kind != "answer_draft" or view.status != "available":
            raise _error("QA_EVIDENCE_UNAVAILABLE", "An available answer draft is required.")
        self._guard_sources(view.sources)
        session, turn = self._locked_turn(session_id, turn_id, expected_attempt)
        # A concurrent publisher may have committed while we waited for the session.
        existing = self.get_answer(session_id, turn_id)
        if existing is not None:
            if existing.answer_snapshot_id != snapshot_id:
                raise _error("QA_ANSWER_CONFLICT", "A different answer is already published.")
            return existing
        if finalize:
            if turn.status != "running" or self.has_later_turn(session_id, turn.turn_no):
                raise _error("QA_EXECUTION_STALE", "Only the current running turn can finalize.")
            # Document locks already precede QA locks. No intermediate commit.
            turn.status = "finalizing"
            self.db.flush()
        if turn.status != "finalizing":
            raise _error("QA_STATE_CONFLICT", "Only a finalizing turn can publish an answer.")
        row = self._snapshot(session_id, turn_id, snapshot_id, lock=True)
        artifact = self.get_artifact(session_id, turn_id, row.artifact_id)
        if row.status != "available" or artifact.attempt_no != expected_attempt:
            raise _error("QA_EVIDENCE_UNAVAILABLE", "Answer draft is deleted or belongs to an old attempt.")
        draft = AnswerDraft.model_validate(row.payload)
        if (draft.outcome == "answer") != bool(view.sources):
            raise _error("QA_SOURCE_REQUIRED", "Answer source provenance is inconsistent.")
        message = QAMessage(
            id=uuid4(), session_id=session_id, turn_id=turn_id, answer_snapshot_id=snapshot_id,
            sequence_no=session.next_message_seq, role="assistant", content=draft.text,
        )
        self.db.add(message)
        session.next_message_seq += 1
        session.updated_at = turn.updated_at = func.now()
        turn.status, turn.outcome, turn.error_code = "completed", draft.outcome, None
        turn.completed_at = func.now()
        self.db.flush()
        return message

    def record_retrieval(
        self, session_id: UUID, turn_id: UUID, message_id: UUID, *, expected_attempt: int,
        evidence_generation: int, query: str, top_k: int, metrics: PersistenceMetrics | dict[str, Any] | None = None,
    ) -> RetrievalLog:
        details = _metrics(metrics)
        if (type(evidence_generation) is not int or evidence_generation < 1 or not isinstance(query, str)
                or not query.strip() or type(top_k) is not int or not 1 <= top_k <= 50):
            raise _error("QA_LOG_INVALID", "Invalid retrieval log fields.", 422)
        _, turn = self._locked_turn(session_id, turn_id, expected_attempt)
        if turn.status != "running":
            raise _error("QA_STATE_CONFLICT", "Retrieval logs require a running turn.")
        message = self.db.scalar(select(QAMessage).where(
            QAMessage.id == message_id, QAMessage.session_id == session_id,
            QAMessage.turn_id == turn_id, QAMessage.role == "user",
        ))
        if message is None:
            raise _error("QA_MESSAGE_NOT_FOUND", "User message not found in this turn.", 404)
        row = self.db.scalar(select(RetrievalLog).where(
            RetrievalLog.turn_id == turn_id, RetrievalLog.evidence_generation == evidence_generation,
        ))
        if row is not None:
            if (row.message_id, row.query, row.top_k, row.result_summary) != (message_id, query, top_k, details):
                raise _error("QA_LOG_CONFLICT", "Retrieval generation has different saved metrics.")
            return row
        row = RetrievalLog(
            id=uuid4(), session_id=session_id, turn_id=turn_id, message_id=message_id,
            evidence_generation=evidence_generation, query=query, top_k=top_k, retrieval_type="hybrid",
            result_summary=details, latency_ms=details.get("latency_ms"),
        )
        self.db.add(row)
        self.db.flush()
        return row
