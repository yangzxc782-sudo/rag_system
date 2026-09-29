"""Synchronous HTTP application service over the accepted M1-M3 components."""
from uuid import UUID

from app.core.errors import BusinessError
from app.rag.query_rewrite_prompt import current_rewrite_policy
from app.db.conversation_lock import ThreadExecutionLocks, ThreadLockLost
from app.schemas.conversations import (
    ConversationMessage, MessageHistoryResponse, RequestStatusResponse, SessionDetailResponse,
    SessionListResponse, TurnCreateRequest,
)
from app.services import conversation_recovery as recovery
from app.services.conversation_history import decode_cursor, encode_cursor, published_answer, session_view
from app.services.conversation_repository import ConversationError, ConversationRepository, fingerprint


class Conversations:
    def __init__(self, graph, *, locks=None, execution_hook=None):
        if graph.rag_nodes is None:
            raise ConversationError("QA_GRAPH_VERSION_UNSUPPORTED", "Conversation API requires the M3 graph.", status_code=503)
        self.graph, self.session_factory = graph, graph.session_factory
        self.settings = graph.nodes.settings
        self.locks = locks or ThreadExecutionLocks(self.settings)
        self.execution_hook = execution_hook  # Test fault injection, never persisted state.

    def close(self):
        self.locks.close()

    def _hook(self, name, graph=None, lease=None):
        if self.execution_hook:
            self.execution_hook(name, graph, lease)

    def create_session(self, request):
        with self.session_factory() as db, db.begin():
            row = ConversationRepository(db).create_session(request.request_id, title="新会话")
            result = session_view(row)
        return result

    def list_sessions(self, *, limit=50, cursor=None):
        with self.session_factory() as db, db.begin():
            rows = ConversationRepository(db).list_sessions(limit=limit + 1, before=decode_cursor(cursor))
            return SessionListResponse(items=[session_view(r) for r in rows[:limit]],
                next_cursor=encode_cursor(rows[limit - 1]) if len(rows) > limit else None)

    def rename_session(self, sid, request):
        with self.session_factory() as db, db.begin():
            result = session_view(ConversationRepository(db).rename_session(sid, request.title))
        return result

    def session_detail(self, sid):
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            result = session_view(repo.get_session(sid))
            turn = repo.active_turn(sid)
            rid = turn.request_id if turn else None
        return SessionDetailResponse(**result.model_dump(),
            active_request=self.request_status(sid, rid) if rid else None)

    def messages(self, sid, *, limit=50, before_seq=None):
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            rows = repo.list_messages(sid, limit=limit + 1, before_seq=before_seq)
            has_more = len(rows) > limit
            rows = rows[-limit:]
            items = []
            for row in rows:
                turn = repo.get_turn(sid, row.turn_id) if row.turn_id else None
                items.append(ConversationMessage(message_id=row.id, sequence_no=row.sequence_no,
                    role=row.role, content=row.content, created_at=row.created_at, turn_id=row.turn_id,
                    request_id=turn.request_id if turn else None, status=turn.status if turn else None,
                    outcome=turn.outcome if turn else None,
                    result=published_answer(repo, turn) if turn and row.role == "assistant" else None))
            return MessageHistoryResponse(thread_id=sid, items=items,
                next_before_seq=rows[0].sequence_no if has_more else None)

    def _status(self, repo, sid, rid, *, active=False):
        repo.get_session(sid)
        turn = repo.get_turn_by_request(sid, rid)
        if turn is None:
            raise ConversationError("QA_REQUEST_NOT_FOUND", "Request not found in this thread.", status_code=404)
        user = repo.get_user_message(sid, turn.id)
        assistant = repo.get_answer(sid, turn.id) if turn.status == "completed" else None
        return RequestStatusResponse(thread_id=sid, turn_id=turn.id, request_id=rid, status=turn.status,
            outcome=turn.outcome, error_code=("QA_REWRITE_STRATEGY_UNSUPPORTED"
                if turn.status != "completed" and not recovery.policy_supported(repo, turn) else turn.error_code),
            can_retry=recovery.can_retry(repo, turn, active=active), execution_active=active and turn.status != "completed",
            status_url=f"{self.settings.api_v1_prefix}/rag/sessions/{sid}/requests/{rid}",
            user_message_id=user.id, assistant_message_id=assistant.id if assistant else None,
            result=published_answer(repo, turn) if assistant else None,
            input=TurnCreateRequest(request_id=rid, question=turn.question,
                                    limit=turn.retrieval_limit, document_id=turn.document_id))

    def request_status(self, sid, rid):
        # Probe the real lock; elapsed client time is never evidence of failure.
        # GET reconciles an orphan to needs_recovery but never calls a model.
        with self.locks.acquire(sid) as lease:
            factory = lease.session_factory if lease else self.session_factory
            checkpoint = None
            if lease:
                with factory() as db, db.begin():
                    repo = ConversationRepository(db)
                    repo.get_session(sid)
                    turn = repo.get_turn_by_request(sid, rid)
                    recovery.reject_legacy(repo, turn)
                if turn and turn.status in {"running", "finalizing"}:
                    checkpoint = self.graph.bind_execution(lease).get_state(sid)
            with factory() as db, db.begin():
                repo = ConversationRepository(db)
                repo.get_session(sid)
                turn = repo.get_turn_by_request(sid, rid)
                if turn and lease:
                    if (checkpoint and recovery.same_execution(checkpoint.values, turn)
                            and checkpoint.values.get("terminal_status") == "needs_recovery"):
                        recovery.record_failure(repo, turn, checkpoint.values.get("error_code") or "QA_STAGE_FAILED")
                    else:
                        recovery.mark_interrupted(repo, turn)
                return self._status(repo, sid, rid, active=lease is None)

    def _validate_request(self, repo, sid, request, limit):
        repo.get_session(sid)
        row = repo.get_turn_by_request(sid, request.request_id)
        identity = fingerprint({"question": request.question, "limit": limit,
                                "document_id": str(request.document_id) if request.document_id else None})
        if row and row.request_fingerprint != identity:
            raise ConversationError("IDEMPOTENCY_CONFLICT", "The request_id already has different parameters.", status_code=409)
        return row

    def submit(self, sid: UUID, request):
        limit = request.limit if request.limit is not None else self.settings.rag_top_k
        if not 1 <= limit <= 50 or len(request.question.encode("utf-8")) > self.settings.conversation_question_max_bytes:
            raise ConversationError("QA_REQUEST_INVALID", "Question or retrieval limit exceeds configuration.", status_code=422)
        with self.session_factory() as db, db.begin():
            row = self._validate_request(ConversationRepository(db), sid, request, limit)
            if row and row.status == "completed":
                return 200, self._status(ConversationRepository(db), sid, request.request_id)
        with self.locks.acquire(sid) as lease:
            if lease is None:
                with self.session_factory() as db, db.begin():
                    repo = ConversationRepository(db)
                    row = self._validate_request(repo, sid, request, limit)
                    if row and row.status == "completed":
                        return 200, self._status(repo, sid, request.request_id)
                    active = repo.active_turn(sid)
                    if row and active and active.id == row.id:
                        return 202, self._status(repo, sid, request.request_id, active=True)
                raise ConversationError("THREAD_BUSY", "Another request owns this thread.", status_code=409)
            graph = self.graph.bind_execution(lease)
            with lease.session_factory() as db, db.begin():
                repo = ConversationRepository(db)
                row = self._validate_request(repo, sid, request, limit)
                if row and row.status == "completed":
                    return 200, self._status(repo, sid, request.request_id)
                recovery.reject_legacy(repo, repo.active_turn(sid))
                row = repo.start_turn(sid, request.request_id, request.question,
                                     limit=limit, document_id=request.document_id, rewrite_policy=current_rewrite_policy())
            publishing = False
            try:
                self._hook("after_user_commit", graph, lease)
                with lease.session_factory() as db, db.begin():
                    repo = ConversationRepository(db)
                    repo.require_rewrite_policy(repo.get_turn(sid, row.id), current_rewrite_policy())
                saved = graph.get_state(sid)
                with lease.session_factory() as db, db.begin():
                    repo = ConversationRepository(db)
                    row = recovery.prepare_retry(repo, repo.get_turn(sid, row.id), saved)
                self._hook("before_graph", graph, lease)
                if recovery.same_execution(saved.values, row) and saved.values.get("terminal_status") == "result_staged" and not saved.next:
                    state = saved.values
                elif recovery.same_execution(saved.values, row) and saved.next:
                    state = graph.resume(sid, row.id, row.request_id, row.attempt_no)
                else:
                    state = graph.invoke(sid, row.id, row.request_id, row.attempt_no)
                if state.get("terminal_status") == "needs_recovery":
                    raise ConversationError(state.get("error_code") or "QA_STAGE_FAILED",
                                            "Conversation stage failed.",
                                            status_code=recovery.REWRITE_ERROR_STATUS.get(state.get("error_code"), 503))
                publishing = True
                self._hook("after_terminal_checkpoint", graph, lease)
                staged = recovery.confirm_terminal(graph, row)
                self._hook("before_publish", graph, lease)
                with lease.session_factory() as db, db.begin():
                    repo = ConversationRepository(db)
                    # No QA lock before publish_answer: it first locks every
                    # document source, then rechecks attempt and draft under QA locks.
                    repo.publish_answer(sid, row.id, staged.draft_snapshot_id,
                                        expected_attempt=row.attempt_no, finalize=True)
                    self._hook("before_publication_commit", graph, lease)
                self._hook("after_publication_commit", graph, lease)
                with lease.session_factory() as db, db.begin():
                    return 200, self._status(ConversationRepository(db), sid, request.request_id)
            except ThreadLockLost:
                raise
            except Exception as exc:
                # Never use a replacement connection to save this executor's
                # outcome after losing its physical lock/session.
                lease.assert_alive()
                with lease.session_factory() as db, db.begin():
                    repo = ConversationRepository(db)
                    current = repo.get_turn(sid, row.id)
                    if current.attempt_no != row.attempt_no:
                        raise ConversationError("QA_EXECUTION_STALE", "Attempt is no longer current.", status_code=409) from None
                    if current.status in {"running", "finalizing"}:
                        if isinstance(exc, BusinessError) and exc.code not in recovery.INTERRUPTED:
                            recovery.record_failure(repo, current, exc.code)
                        else:
                            recovery.mark_interrupted(repo, current, publication=publishing)
                if isinstance(exc, BusinessError):
                    raise
                raise ConversationError("QA_PUBLICATION_INTERRUPTED" if publishing else "QA_EXECUTION_INTERRUPTED",
                                        "Request interrupted; query status or retry the same request_id.", status_code=503) from None
