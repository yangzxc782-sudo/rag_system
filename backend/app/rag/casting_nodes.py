"""V3 route/tool adapters. ToolNode messages never enter outer graph state."""
import json
import re
from dataclasses import replace
from uuid import UUID

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import ToolNode
from pydantic import ValidationError
from sqlalchemy import select

from app.casting.execution_protocol import CastingExecutionError, digest
from app.core.errors import BusinessError
from app.llm.messages import LLMFunctionCall, LLMMessage, LLMToolCall, parse_tool_arguments
from app.models.casting_design_run import CastingDesignRun
from app.models.qa_turn import QATurn
from app.models.qa_turn_artifact import QATurnArtifact
from app.rag.casting_prompt import PROMPT_FINGERPRINT, TOOL_NAME, requires_updated_input, routing_request, tool_result_request
from app.rag.casting_projection import project_result, projection_bytes
from app.rag.casting_tool import casting_tool
from app.schemas.casting_graph import (
    CASTING_ARTIFACT_ADAPTER, ROUTE_KEY, TOOL_KEY, CastingRouteDetails, CastingToolDetails, CastingToolInput,
)
from app.services.casting_repository import CastingRepository
from app.services.conversation_repository import ConversationError, ConversationRepository, fingerprint


def route_result(message: LLMMessage, context: dict) -> CastingRouteDetails:
    """Only a validated native tool call can authorize calculation."""
    common = dict(effective_input_file_id=context["effective_input_file_id"],
                  prompt_fingerprint=PROMPT_FINGERPRINT, context_fingerprint=fingerprint(context))
    try:
        if message.tool_calls:
            if len(message.tool_calls) != 1:
                raise ValueError("Multiple tools")
            call = message.tool_calls[0]
            args = CastingToolInput.model_validate(parse_tool_arguments(call.function.arguments))
            if call.function.name != TOOL_NAME or args.input_file_id != context["effective_input_file_id"]:
                raise ValueError("Tool name or frozen input mismatch")
            if requires_updated_input(context):
                return CastingRouteDetails(route="input_required", **common)
            return CastingRouteDetails(route="calculate", tool_call_id=call.id, **common)
        if len(message.content) != 1:
            raise ValueError("Missing route")
        obj = parse_tool_arguments(message.content[0].text)
        if obj.get("route") == "explain_existing":
            if (set(obj) - {"route", "source_run_id", "candidate_rank"} or not {"route", "source_run_id"} <= set(obj)
                    or obj["source_run_id"] not in {x["run_id"] for x in context["recent_runs"]}):
                raise ValueError("Unknown historical result")
            if obj.get("candidate_rank") is not None:
                run = next(x for x in context["recent_runs"] if x["run_id"] == obj["source_run_id"])
                if type(obj["candidate_rank"]) is not int or not 1 <= obj["candidate_rank"] <= run.get("candidate_count", 0):
                    raise ValueError("Candidate outside saved result")
        elif set(obj) != {"route"} or obj.get("route") not in {"rag", "input_required", "clarify_selection"}:
            raise ValueError("Unsupported route")
        return CastingRouteDetails(**obj, **common)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ConversationError("CASTING_TOOL_CALL_INVALID", "Model returned an invalid casting route or tool call.", status_code=502) from None


def assistant_call(route: CastingRouteDetails) -> LLMMessage:
    if route.route != "calculate":
        raise ValueError("Route is not a tool invocation")
    return LLMMessage("assistant", tool_calls=(LLMToolCall(route.tool_call_id,
        LLMFunctionCall(TOOL_NAME, json.dumps({"input_file_id": route.effective_input_file_id}))),))


class CastingNodes:
    def __init__(self, base, service):
        self.base, self.service = base, service
        self.session_factory, self.provider = base.session_factory, base.rewriter.provider
        self.after_stage_commit = None

    def node(self, name):
        method = getattr(self, name)
        def run(state, config: RunnableConfig):
            try:
                return method(state, config)
            except (BusinessError, CastingExecutionError) as exc:
                code = exc.code if re.fullmatch(r"[A-Z0-9_]{1,100}", exc.code) else "CASTING_STAGE_FAILED"
            except (ValidationError, ValueError, TypeError, KeyError):
                code = "CASTING_STAGE_INVALID"
            return {"stage": "failed", "terminal_status": "needs_recovery", "outcome": None, "error_code": code}
        return run

    @staticmethod
    def _fingerprint(identity, key, parent=None):
        return fingerprint({"request": identity.input_fingerprint, "turn_id": str(identity.turn_id),
            "request_id": str(identity.request_id), "thread_id": str(identity.thread_id),
            "attempt": identity.attempt_no, "graph": identity.graph_version, "key": key,
            "parent": str(parent) if parent else None, "prompt": PROMPT_FINGERPRINT})

    def _identity(self, state, config):
        identity = self.base._identity(state, config)
        if state["state_schema_version"] != 3 or self.service is None:
            raise ConversationError("CASTING_SERVICE_UNAVAILABLE", "Casting v3 service is not available.", status_code=503)
        return identity

    def _existing(self, repo, identity, key, parent=None):
        row = repo.get_artifact_by_key(identity.thread_id, identity.turn_id, attempt_no=identity.attempt_no, key=key)
        if row is None:
            return None
        if (row.kind != "context" or row.schema_version != 3 or row.parent_artifact_id != parent
                or row.input_fingerprint != self._fingerprint(identity, key, parent)):
            raise ConversationError("CASTING_STAGE_INVALID", "Casting artifact identity mismatch.", status_code=409)
        if row.content_fingerprint != fingerprint({"kind": row.kind, "input": row.input_fingerprint, "details": row.details,
                "parent": str(parent) if parent else None, "version": row.schema_version}):
            raise ConversationError("CASTING_PROVENANCE_INVALID", "Casting artifact content changed.", status_code=409)
        details = CASTING_ARTIFACT_ADAPTER.validate_json(json.dumps(row.details))
        if (key == ROUTE_KEY) != isinstance(details, CastingRouteDetails):
            raise ConversationError("CASTING_STAGE_INVALID", "Casting artifact type mismatch.", status_code=409)
        return row.id, details

    def _save(self, identity, key, details, parent=None):
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            identity.validate(repo, lock=True)
            row = repo.save_artifact(identity.thread_id, identity.turn_id, expected_attempt=identity.attempt_no,
                key=key, kind="context", schema_version=3, details=details, parent_artifact_id=parent,
                input_fingerprint=self._fingerprint(identity, key, parent))
            ident = row.id
        if self.after_stage_commit:
            self.after_stage_commit(key)
        return ident

    def _context(self, repo, identity, state):
        turn = identity.validate(repo)
        fid = turn.effective_casting_input_file_id
        if (str(fid) if fid else None) != state["effective_input_file_id"]:
            raise ConversationError("CASTING_TOOL_INPUT_MISMATCH", "State input differs from the frozen turn.", status_code=409)
        if fid is not None:
            CastingRepository(repo.db).file(identity.thread_id, fid, ready=True, input_only=True)
        runs = repo.db.execute(select(CastingDesignRun.id, CastingDesignRun.candidate_count, QATurn.id.label("turn_id")).join(QATurn,
            (QATurn.id == CastingDesignRun.turn_id) & (QATurn.session_id == CastingDesignRun.session_id)).where(
                CastingDesignRun.session_id == identity.thread_id, QATurn.turn_no < turn.turn_no,
                CastingDesignRun.status.in_(["succeeded", "no_feasible_candidate"])
            ).order_by(QATurn.turn_no.desc()).limit(3)).all()
        return dict(question=turn.question, effective_input_file_id=str(fid) if fid else None,
            input_source="current_message" if turn.requested_casting_input_file_id else "session_history" if fid else "none",
            recent_runs=[{"run_id": str(r.id), "turn_id": str(r.turn_id), "candidate_count": r.candidate_count} for r in runs])

    def route_casting(self, state, config):
        identity = self._identity(state, config)
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            context = self._context(repo, identity, state)
            saved = self._existing(repo, identity, ROUTE_KEY)
            prior = None
            if saved is None and identity.attempt_no > 1:
                previous = repo.db.scalar(select(QATurnArtifact.attempt_no).where(
                    QATurnArtifact.session_id == identity.thread_id, QATurnArtifact.turn_id == identity.turn_id,
                    QATurnArtifact.artifact_key == ROUTE_KEY, QATurnArtifact.attempt_no < identity.attempt_no
                ).order_by(QATurnArtifact.attempt_no.desc()).limit(1))
                if previous:
                    prior = self._existing(repo, replace(identity, attempt_no=previous), ROUTE_KEY)
        if saved:
            artifact_id, details = saved
            if details.context_fingerprint != fingerprint(context) or details.prompt_fingerprint != PROMPT_FINGERPRINT:
                raise ConversationError("CASTING_POLICY_CHANGED", "Frozen routing context/policy changed.", status_code=409)
        else:
            if prior:
                details = prior[1]
                if details.context_fingerprint != fingerprint(context) or details.prompt_fingerprint != PROMPT_FINGERPRINT:
                    raise ConversationError("CASTING_POLICY_CHANGED", "Saved route no longer matches this execution.", status_code=409)
            else:
                # External model invocation outside every business SQL transaction.
                request = routing_request(context)
                result = self.provider.generate(request)
                details = route_result(result.message, context)
            artifact_id = self._save(identity, ROUTE_KEY, details)
        return {"route_artifact_id": str(artifact_id), "casting_route": details.route,
                "source_run_id": details.source_run_id, "stage": "casting_routed", "error_code": None,
                "terminal_status": None if details.route in {"rag", "calculate"} else "casting_ready"}

    def _route(self, state, config):
        identity = self._identity(state, config)
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            context = self._context(repo, identity, state)
            saved = self._existing(repo, identity, ROUTE_KEY)
            if (saved is None or str(saved[0]) != state["route_artifact_id"]
                    or saved[1].context_fingerprint != fingerprint(context) or saved[1].route != state["casting_route"]):
                raise ConversationError("CASTING_STAGE_INVALID", "Casting route reference mismatch.", status_code=409)
        return identity, saved[0], saved[1]

    def execute_casting_tool(self, state, config: RunnableConfig):
        identity, parent, route = self._route(state, config)
        if route.route != "calculate":
            raise ConversationError("CASTING_TOOL_CALL_INVALID", "This route does not authorize computation.", status_code=409)
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            identity.validate(repo)
            saved = self._existing(repo, identity, TOOL_KEY, parent)
        if saved:
            artifact_id, details = saved
            projection = project_result(self.service, identity.thread_id, UUID(details.run_id))
            if digest(projection_bytes(projection)) != details.projection_sha256:
                raise ConversationError("CASTING_OUTPUT_INVALID", "Persisted tool result changed.", status_code=409)
        else:
            call = assistant_call(route).tool_calls[0]
            ephemeral = AIMessage(content="", tool_calls=[{"name": call.function.name,
                "args": {"input_file_id": route.effective_input_file_id}, "id": call.id, "type": "tool_call"}])
            output = ToolNode([casting_tool(self.service, identity)], handle_tool_errors=False).invoke(
                {"messages": [ephemeral]}, config=config)
            messages = output.get("messages", [])
            if (len(messages) != 1 or not isinstance(messages[0], ToolMessage)
                    or messages[0].tool_call_id != call.id or messages[0].status == "error"):
                raise ConversationError("CASTING_TOOL_RESULT_INVALID", "ToolNode returned invalid result linkage.", status_code=502)
            projection = json.loads(messages[0].content)
            rid = UUID(projection["run_id"])
            run = self.service.get_run(identity.thread_id, rid)
            if run.turn_id != identity.turn_id or str(run.input_file_id) != route.effective_input_file_id:
                raise ConversationError("CASTING_TOOL_RESULT_INVALID", "Tool result belongs to another execution.", status_code=409)
            details = CastingToolDetails(run_id=str(rid), input_file_id=str(run.input_file_id),
                result_file_id=str(run.result_file_id) if run.result_file_id else None, result_sha256=run.result_sha256,
                status=run.status, projection_sha256=digest(projection_bytes(projection)),
                error_code=run.error.code if run.error else None)
            artifact_id = self._save(identity, TOOL_KEY, details, parent)
        return {"tool_artifact_id": str(artifact_id), "source_run_id": details.run_id,
                "stage": "casting_tool_completed", "terminal_status": "casting_ready", "outcome": None, "error_code": None}

    def read_handoff(self, state, config):
        """Internal phase-5 interface. Rehydrate, verify; never publish model text."""
        identity, parent, route = self._route(state, config)
        if route.route != "calculate":
            return {"route": route.route, "source_run_id": route.source_run_id,
                    "message": "请上传或明确选择有效的工程输入 JSON。" if route.route == "input_required" else None}
        with self.session_factory() as db, db.begin():
            saved = self._existing(ConversationRepository(db), identity, TOOL_KEY, parent)
        if saved is None or str(saved[0]) != state["tool_artifact_id"]:
            raise ConversationError("CASTING_RESULT_NOT_READY", "Verified tool result is missing.", status_code=409)
        details = saved[1]
        projection = project_result(self.service, identity.thread_id, UUID(details.run_id))
        if digest(projection_bytes(projection)) != details.projection_sha256:
            raise ConversationError("CASTING_OUTPUT_INVALID", "Tool projection changed.", status_code=409)
        with self.session_factory() as db, db.begin():
            turn = identity.validate(ConversationRepository(db))
            question, inherited = turn.question, turn.requested_casting_input_file_id is None
        return {"route": "calculate", "projection": projection, "input_reused": inherited,
                "model_request": tool_result_request(question, assistant_call(route), projection)}
