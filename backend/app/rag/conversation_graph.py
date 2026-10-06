"""Versioned synchronous graphs. Business message publication belongs to M4."""
from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from app.core.config import Settings
from app.core.errors import BusinessError
from app.rag.conversation_state import (
    ConversationState, ConversationStateV2, ConversationStateV3, ExecutionIdentity, REWRITE_ARTIFACT_KEY,
    CASTING_GRAPH_VERSION, M3_GRAPH_VERSION, StateContractV2, StateContractV3, validate_state,
)
from app.rag.history_budget import HistoryContext, read_rewrite_details, rehydrate_history, select_history
from app.rag.query_rewrite import QueryRewriter, rewrite_input_fingerprint, validate_result
from app.rag.query_rewrite_prompt import PROMPT_FINGERPRINT
from app.schemas.query_rewrite import REWRITE_STRATEGY
from app.services.conversation_repository import ConversationError, ConversationRepository, fingerprint


def artifact_fingerprint(identity: ExecutionIdentity, message_id: UUID, details) -> str:
    return fingerprint({
        "rewrite_strategy": REWRITE_STRATEGY, "prompt": details.prompt_fingerprint,
        "model_input": details.input_fingerprint, "request": identity.input_fingerprint,
        "thread": str(identity.thread_id), "request_id": str(identity.request_id),
        "turn": str(identity.turn_id), "attempt": identity.attempt_no,
        "current_message_id": str(message_id),
        "history_message_ids": [str(value) for value in details.history_message_ids],
        "input_pending_clarification_turn_id": str(details.input_pending_clarification_turn_id) if details.input_pending_clarification_turn_id else None,
    })


class ConversationNodes:
    def __init__(self, session_factory, rewriter: QueryRewriter, settings: Settings,
                 *, after_artifact_commit: Callable[[], None] | None = None):
        self.session_factory, self.rewriter, self.settings = session_factory, rewriter, settings
        self.after_artifact_commit = after_artifact_commit

    def _identity(self, state, config):
        checked = validate_state(state)
        if checked["state_schema_version"] not in {2, 3}:
            raise ConversationError("QA_REWRITE_STRATEGY_UNSUPPORTED", "Legacy graph execution is disabled.", status_code=409)
        if config.get("configurable", {}).get("thread_id") != checked["thread_id"]:
            raise ConversationError("QA_CHECKPOINT_THREAD_MISMATCH", "Graph/config thread mismatch.", status_code=409)
        return ExecutionIdentity.from_state(checked)

    def _reuse(self, repo, identity, turn, artifact):
        if (artifact.kind != "rewrite" or artifact.artifact_key != REWRITE_ARTIFACT_KEY
                or artifact.attempt_no != identity.attempt_no
                or artifact.schema_version != 2):
            raise ConversationError("QA_REWRITE_ARTIFACT_INVALID", "Rewrite artifact version/attempt mismatch.", status_code=409)
        details = read_rewrite_details(artifact)
        if (details.input_bytes > self.settings.conversation_rewrite_max_input_bytes
                or details.input_estimated_tokens > self.settings.conversation_rewrite_max_estimated_tokens):
            raise ConversationError("QA_REWRITE_ARTIFACT_INVALID", "Stored rewrite exceeds the current input budget.", status_code=409)
        current = repo.get_user_message(identity.thread_id, identity.turn_id)
        if (details.prompt_fingerprint != PROMPT_FINGERPRINT
                or artifact.input_fingerprint != artifact_fingerprint(identity, current.id, details)):
            raise ConversationError("QA_REWRITE_ARTIFACT_INVALID", "Rewrite artifact input identity mismatch.", status_code=409)
        context = rehydrate_history(repo, turn, details.history_message_ids, details.input_pending_clarification_turn_id, self.settings)
        if details.input_fingerprint != rewrite_input_fingerprint(turn.question, current.id, context, self.settings):
            raise ConversationError("QA_REWRITE_ARTIFACT_INVALID", "Rewrite input changed.", status_code=409)
        try:
            validate_result(details.result, current.id, context, max_query_bytes=self.settings.conversation_question_max_bytes)
            expected_pending = (context.pending.turn_id if context.pending and details.result.decision != "standalone"
                                and details.result.history_scope == "clarification" else None)
            if details.pending_clarification_turn_id != expected_pending:
                raise ValueError("Inconsistent pending relation")
        except ValueError:
            raise ConversationError("QA_REWRITE_ARTIFACT_INVALID", "Invalid rewrite output references.", status_code=409) from None
        return details

    def load_context(self, state: ConversationState, config) -> dict:
        identity = self._identity(state, config)
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            turn = identity.validate(repo)
            current = repo.get_user_message(identity.thread_id, identity.turn_id)
            if str(current.id) != state["current_message_id"]:
                raise ConversationError("QA_CONTEXT_INVALID", "Current user message identity mismatch.", status_code=409)
            artifact = repo.get_artifact_by_key(identity.thread_id, identity.turn_id,
                                               attempt_no=identity.attempt_no, key=REWRITE_ARTIFACT_KEY)
            if artifact:
                details = self._reuse(repo, identity, turn, artifact)
                ids, pending, exhausted = details.history_message_ids, details.input_pending_clarification_turn_id, False
            else:
                context = select_history(repo, turn, self.settings)
                ids, pending, exhausted = context.message_ids, context.pending.turn_id if context.pending else None, context.budget_exhausted
        return {"stage": "context_loaded", "history_message_ids": [str(value) for value in ids],
                "pending_clarification_turn_id": str(pending) if pending else None,
                "rewrite_artifact_id": str(artifact.id) if artifact else None,
                "terminal_status": None, "outcome": None,
                "error_code": "QA_CONTEXT_BUDGET_EXCEEDED" if exhausted else None}

    def understand_question(self, state: ConversationState, config) -> dict:
        identity = self._identity(state, config)
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            turn = identity.validate(repo)
            current = repo.get_user_message(identity.thread_id, identity.turn_id)
            artifact = repo.get_artifact_by_key(identity.thread_id, identity.turn_id,
                                               attempt_no=identity.attempt_no, key=REWRITE_ARTIFACT_KEY)
            if artifact:
                details = self._reuse(repo, identity, turn, artifact)
                return self._understood(artifact.id, details)
            context = rehydrate_history(repo, turn, [UUID(value) for value in state["history_message_ids"]],
                                        UUID(state["pending_clarification_turn_id"]) if state["pending_clarification_turn_id"] else None,
                                        self.settings)
            context = HistoryContext(context.messages, context.pending, state["error_code"] == "QA_CONTEXT_BUDGET_EXCEEDED")
            question, message_id = turn.question, current.id
        # No open Session, transaction, Document/session lock or checkpoint
        # connection is retained while waiting for the external provider.
        try:
            details = self.rewriter.understand(question, message_id, context).details
        except BusinessError as exc:
            code = exc.code
            if not isinstance(code, str) or not 1 <= len(code) <= 100 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in code):
                code = "QA_STAGE_FAILED"
            return {"stage": "failed", "terminal_status": "needs_recovery", "outcome": None, "error_code": code}
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            turn = identity.validate(repo, lock=True)
            existing = repo.get_artifact_by_key(identity.thread_id, identity.turn_id,
                                               attempt_no=identity.attempt_no, key=REWRITE_ARTIFACT_KEY)
            if existing:
                details = self._reuse(repo, identity, turn, existing)
                artifact_id = existing.id
            else:
                saved = repo.save_artifact(
                    identity.thread_id, identity.turn_id, expected_attempt=identity.attempt_no,
                    key=REWRITE_ARTIFACT_KEY, kind="rewrite", details=details,
                    schema_version=2,
                    input_fingerprint=artifact_fingerprint(identity, message_id, details),
                )
                artifact_id = saved.id
        if self.after_artifact_commit is not None:
            self.after_artifact_commit()
        return self._understood(artifact_id, details)

    @staticmethod
    def _understood(artifact_id, details):
        return {"stage": "understood", "rewrite_artifact_id": str(artifact_id),
                "history_message_ids": [str(value) for value in details.history_message_ids],
                "pending_clarification_turn_id": str(details.pending_clarification_turn_id) if details.pending_clarification_turn_id else None,
                "outcome": "clarification" if details.result.decision == "clarify" else details.result.decision,
                "error_code": None}



def build_conversation_graph(nodes: ConversationNodes, checkpointer, rag_nodes, casting_nodes=None):
    builder = StateGraph(ConversationStateV3 if casting_nodes is not None else ConversationStateV2)
    builder.add_node("load_context", nodes.load_context, input_schema=ConversationStateV2)
    builder.add_node("understand_question", nodes.understand_question, input_schema=ConversationStateV2)
    if casting_nodes is None:
        builder.add_edge(START, "load_context")
    else:
        builder.add_node("route_casting", casting_nodes.node("route_casting"))
        builder.add_node("execute_casting_tool", casting_nodes.node("execute_casting_tool"))
        builder.add_node("generate_casting_answer", casting_nodes.node("generate_casting_answer"))
        builder.add_node("stage_casting_result", casting_nodes.node("stage_casting_result"))
        builder.add_edge(START, "route_casting")
        builder.add_conditional_edges("route_casting",
            lambda s: "error" if s["error_code"] else s["casting_route"],
            {"error": END, "rag": "load_context", "calculate": "execute_casting_tool",
             "input_required": "generate_casting_answer", "explain_existing": "generate_casting_answer", "clarify_selection": "generate_casting_answer"})
        builder.add_conditional_edges("execute_casting_tool", lambda s: "error" if s["error_code"] else "next",
            {"error": END, "next": "generate_casting_answer"})
        builder.add_conditional_edges("generate_casting_answer", lambda s: "error" if s["error_code"] else "next",
            {"error": END, "next": "stage_casting_result"})
        builder.add_edge("stage_casting_result", END)
    builder.add_edge("load_context", "understand_question")
    for name in ("retrieve_and_rerank", "build_evidence", "generate_answer", "stage_result", "stage_clarification"):
        builder.add_node(name, rag_nodes.node(name))
    builder.add_conditional_edges("understand_question",
        lambda s: "error" if s["error_code"] else "clarify" if s["outcome"] == "clarification" else "retrieve",
        {"error": END, "clarify": "stage_clarification", "retrieve": "retrieve_and_rerank"})
    builder.add_conditional_edges("retrieve_and_rerank", lambda s: "error" if s["error_code"] else "next",
                                  {"error": END, "next": "build_evidence"})
    builder.add_conditional_edges("build_evidence", lambda s: "error" if s["error_code"] else s["outcome"],
                                  {"error": END, "no_context": "stage_result", "answer": "generate_answer"})
    builder.add_conditional_edges("generate_answer", lambda s: "error" if s["error_code"] else "next",
                                  {"error": END, "next": "stage_result"})
    builder.add_edge("stage_result", END)
    builder.add_edge("stage_clarification", END)
    return builder.compile(checkpointer=checkpointer)


class ConversationGraph:
    """Internal synchronous entry point, not an HTTP request/recovery coordinator."""
    def __init__(self, session_factory, checkpoints, rewriter: QueryRewriter, settings: Settings, *, graph_retrieval=None, casting_service=None):
        self.session_factory, self.checkpoints = session_factory, checkpoints
        self.nodes = ConversationNodes(session_factory, rewriter, settings)
        self.casting_service = casting_service
        if settings.conversation_graph_version not in {M3_GRAPH_VERSION, CASTING_GRAPH_VERSION}:
            raise ConversationError("QA_REWRITE_STRATEGY_UNSUPPORTED", "Legacy graph execution is disabled.", status_code=409)
        from app.rag.conversation_nodes import ConversationRagNodes
        self.rag_nodes = ConversationRagNodes(self.nodes, graph_retrieval=graph_retrieval)

    def bind_execution(self, lease):
        """Reuse the same graph/provider with execution-scoped DB resources."""
        bound = ConversationGraph(lease.session_factory, lease, self.nodes.rewriter, self.nodes.settings,
                                  graph_retrieval=self.rag_nodes.evidence_service.graph_retrieval,
                                  casting_service=self.casting_service.bind_session_factory(lease.session_factory) if self.casting_service else None)
        return bound

    def _casting_nodes(self):
        from app.rag.casting_answer_nodes import CastingAnswerNodes
        return CastingAnswerNodes(self.nodes, self.casting_service)

    def _build(self, saver, graph_version):
        casting = self._casting_nodes() if graph_version == CASTING_GRAPH_VERSION else None
        return build_conversation_graph(self.nodes, saver, self.rag_nodes, casting)

    def _initial(self, thread_id: UUID, turn_id: UUID, request_id: UUID, attempt_no: int):
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            turn = repo.get_turn(thread_id, turn_id)
            version = (turn.graph_version or M3_GRAPH_VERSION) if self.nodes.settings.casting_design_enabled else M3_GRAPH_VERSION
            if version not in {M3_GRAPH_VERSION, CASTING_GRAPH_VERSION}:
                raise ConversationError("QA_GRAPH_VERSION_UNSUPPORTED", "Unsupported frozen graph version.", status_code=409)
            if version == CASTING_GRAPH_VERSION and self.casting_service is None:
                raise ConversationError("CASTING_SERVICE_UNAVAILABLE", "Casting graph needs its persisted calculation service.", status_code=503)
            identity = ExecutionIdentity(thread_id, turn_id, request_id, attempt_no, turn.request_fingerprint, version)
            identity.validate(repo)
            message = repo.get_user_message(thread_id, turn_id)
            contract = StateContractV3 if version == CASTING_GRAPH_VERSION else StateContractV2
            extra = {"effective_input_file_id": str(turn.effective_casting_input_file_id) if turn.effective_casting_input_file_id else None} if version == CASTING_GRAPH_VERSION else {}
            state = contract(thread_id=str(thread_id), turn_id=str(turn_id), request_id=str(request_id),
                                  attempt_no=attempt_no, input_fingerprint=turn.request_fingerprint,
                                  current_message_id=str(message.id), evidence_generation=attempt_no, **extra).model_dump()
        return identity, state

    def invoke(self, thread_id: UUID, turn_id: UUID, request_id: UUID, attempt_no: int) -> ConversationState:
        identity, initial = self._initial(thread_id, turn_id, request_id, attempt_no)
        saved = self.get_state(thread_id)
        if (saved.values and saved.values.get("turn_id") == str(turn_id)
                and saved.values.get("graph_version") != identity.graph_version):
            raise ConversationError("QA_GRAPH_VERSION_MISMATCH", "A saved execution cannot be reinterpreted as another graph version.", status_code=409)
        if (saved.values and ExecutionIdentity.from_state(saved.values) == identity
                and saved.values.get("terminal_status") == "needs_recovery"):
            # M4 owns explicit new-attempt retry; do not rerun a failed attempt.
            return validate_state(saved.values)
        saver = self.checkpoints.saver(thread_id, self.session_factory, identity=identity)
        graph = self._build(saver, identity.graph_version)
        # Local business history never opts into external tracing via ambient env.
        with tracing_context(enabled=False):
            # This graph is linear. Serialize its checkpoint worker submissions:
            # upstream sync durability waits for checkpoint.put, but ordinary
            # pending put_writes can otherwise overlap the next model call.
            # Each invocation owns its executor; different threads stay parallel.
            result = graph.invoke(initial, {"configurable": {"thread_id": str(thread_id)},
                                          "recursion_limit": 16, "max_concurrency": 1}, durability="sync")
        return validate_state(result)

    def get_state(self, thread_id: UUID):
        with self.session_factory() as db, db.begin():
            ConversationRepository(db).get_session(thread_id)
        # Read with a channel superset so a stored v3 checkpoint is never silently
        # filtered through v2 channels. No routing/model/tool runs during get_state.
        graph = self._build(self.checkpoints.saver(thread_id, self.session_factory), CASTING_GRAPH_VERSION)
        with tracing_context(enabled=False):
            result = graph.get_state({"configurable": {"thread_id": str(thread_id)}})
        if result.values:
            checked = validate_state(result.values)
            if checked["thread_id"] != str(thread_id):
                raise ConversationError("QA_CHECKPOINT_THREAD_MISMATCH", "Stored state belongs to another thread.", status_code=409)
        return result

    def resume(self, thread_id: UUID, turn_id: UUID, request_id: UUID, attempt_no: int) -> ConversationState:
        identity, _ = self._initial(thread_id, turn_id, request_id, attempt_no)
        saved = self.get_state(thread_id)
        if not saved.values or ExecutionIdentity.from_state(saved.values) != identity:
            raise ConversationError("QA_EXECUTION_STALE", "Checkpoint does not match this business attempt.", status_code=409)
        if saved.values["state_schema_version"] not in {2, 3}:
            raise ConversationError("QA_REWRITE_STRATEGY_UNSUPPORTED", "Legacy checkpoints cannot resume.", status_code=409)
        graph = self._build(self.checkpoints.saver(thread_id, self.session_factory, identity=identity), identity.graph_version)
        with tracing_context(enabled=False):
            result = graph.invoke(None, {"configurable": {"thread_id": str(thread_id)},
                                       "recursion_limit": 16, "max_concurrency": 1}, durability="sync")
        return validate_state(result)

    def get_casting_result(self, thread_id: UUID, turn_id: UUID, request_id: UUID, attempt_no: int):
        identity, _ = self._initial(thread_id, turn_id, request_id, attempt_no)
        saved = self.get_state(thread_id)
        if (saved.next or not saved.values or ExecutionIdentity.from_state(saved.values) != identity
                or saved.values.get("terminal_status") not in {"casting_ready", "result_staged"}):
            raise ConversationError("CASTING_RESULT_NOT_READY", "Casting handoff lacks a terminal checkpoint.", status_code=409)
        return self._casting_nodes().read_handoff(saved.values, {"configurable": {"thread_id": str(thread_id)}})

    def get_result(self, thread_id: UUID, turn_id: UUID, request_id: UUID, attempt_no: int):
        """Read staged data and separately report whether the terminal checkpoint exists."""
        from dataclasses import replace
        saved = self.get_state(thread_id)
        if saved.values.get("outcome") == "casting_design":
            identity, _ = self._initial(thread_id, turn_id, request_id, attempt_no)
            if ExecutionIdentity.from_state(saved.values) != identity:
                raise ConversationError("QA_EXECUTION_STALE", "Engineering result belongs to another attempt.", status_code=409)
            result = self._casting_nodes().read_result(saved.values, {"configurable": {"thread_id": str(thread_id)}})
        else:
            result = self.rag_nodes.read_result(thread_id, turn_id, request_id, attempt_no)
        complete = (not saved.next and saved.values.get("turn_id") == str(turn_id)
                    and saved.values.get("attempt_no") == attempt_no
                    and saved.values.get("result_artifact_id") == str(result.result_artifact_id)
                    and saved.values.get("terminal_status") == "result_staged")
        return replace(result, checkpoint_complete=complete)
