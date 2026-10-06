"""SQL-only provenance checks. Object bytes are verified before the publish transaction."""
import json

from app.casting.execution_protocol import digest
from app.schemas.casting_answer import (
    GENERATION_KEY, RESULT_KEY, CastingAnswerDraft, CastingGenerationDetails, CastingResultDetails,
)
from app.schemas.casting_graph import CASTING_GRAPH_VERSION, ROUTE_KEY, TOOL_KEY, CastingRouteDetails, CastingToolDetails


def invalid():
    from app.services.conversation_repository import ConversationError
    raise ConversationError("CASTING_PROVENANCE_INVALID", "工程回答来源与已保存计算不一致。", status_code=409)


def typed_artifact(repo, turn, aid, model, key, kind):
    from app.services.conversation_repository import fingerprint
    row = repo.get_artifact(turn.session_id, turn.id, aid)
    if (row.attempt_no, row.artifact_key, row.kind, row.schema_version) != (turn.attempt_no, key, kind, 3):
        invalid()
    if row.content_fingerprint != fingerprint({"kind": row.kind, "input": row.input_fingerprint, "details": row.details,
            "parent": str(row.parent_artifact_id) if row.parent_artifact_id else None, "version": row.schema_version}):
        invalid()
    return row, model.model_validate_json(json.dumps(row.details))


def validate_source(repo, turn, draft: CastingAnswerDraft, generation):
    from app.services.casting_repository import CastingRepository
    if turn.graph_version != CASTING_GRAPH_VERSION:
        invalid()
    route_row, route = typed_artifact(repo, turn, draft.route_artifact_id, CastingRouteDetails, ROUTE_KEY, "context")
    info = draft.casting
    explanation = info.summary_mode == "llm_explanation"
    if explanation:
        if (route.route != "explain_existing" or info.result_status not in {"success", "no_feasible_candidate"}
                or draft.renderer_version != "casting_explanation_v1"):
            invalid()
    elif draft.renderer_version != "casting_facts_v1":
        invalid()
    if (route_row.parent_artifact_id is not None or route.route != info.route
            or route.effective_input_file_id != (str(turn.effective_casting_input_file_id) if turn.effective_casting_input_file_id else None)
            or route.candidate_rank != info.candidate_rank):
        invalid()
    if generation is not None:
        row, details = typed_artifact(repo, turn, generation.id, CastingGenerationDetails, GENERATION_KEY, "generation")
        if (row.parent_artifact_id != (draft.tool_artifact_id or draft.route_artifact_id)
                or details.route_artifact_id != draft.route_artifact_id or details.tool_artifact_id != draft.tool_artifact_id
                or details.facts_sha256 != draft.facts_sha256 or details.summary_mode != info.summary_mode):
            invalid()
        if details.answer_text_sha256 != (digest(draft.text.encode("utf-8")) if explanation else None):
            invalid()
    if route.route in {"input_required", "clarify_selection"}:
        if (info.result_status != route.route or info.run_id is not None or info.result_file_id is not None
                or info.result_sha256 is not None or draft.tool_artifact_id is not None
                or info.input_file_id != turn.effective_casting_input_file_id):
            invalid()
        return
    if info.run_id is None:
        invalid()
    storage = CastingRepository(repo.db)
    run = storage.run(turn.session_id, info.run_id)
    if route.route == "calculate":
        if run.turn_id != turn.id or run.input_file_id != turn.effective_casting_input_file_id or draft.tool_artifact_id is None:
            invalid()
        row, tool = typed_artifact(repo, turn, draft.tool_artifact_id, CastingToolDetails, TOOL_KEY, "context")
        if (row.parent_artifact_id != route_row.id or tool.run_id != str(run.id) or tool.status != run.status
                or tool.input_file_id != str(run.input_file_id) or tool.result_sha256 != run.result_sha256
                or tool.result_file_id != (str(run.result_file_id) if run.result_file_id else None)):
            invalid()
    elif route.route == "explain_existing":
        prior = repo.get_turn(turn.session_id, run.turn_id)
        if (route.source_run_id != str(run.id) or prior.turn_no >= turn.turn_no
                or run.status not in {"succeeded", "no_feasible_candidate"} or draft.tool_artifact_id is not None):
            invalid()
    else:
        invalid()
    expected = {"result_status": "success" if run.status == "succeeded" else run.status,
        "input_file_id": run.input_file_id, "result_file_id": run.result_file_id, "result_sha256": run.result_sha256,
        "rule_id": run.rule_id, "rule_version": run.rule_version, "rule_sha256": run.rule_sha256,
        "candidate_count": run.candidate_count, "recommended_candidate_id": run.recommended_candidate_id,
        "input_reused": route.route == "explain_existing" or turn.requested_casting_input_file_id is None}
    if any(getattr(info, key) != value for key, value in expected.items()):
        invalid()
    source = storage.file(turn.session_id, run.input_file_id, ready=True, input_only=True)
    if source.sha256 != run.input_sha256:
        invalid()
    if run.result_file_id:
        result = storage.file(turn.session_id, run.result_file_id, ready=True)
        if (result.kind != "recommendation" or result.run_id != run.id or result.sha256 != run.result_sha256
                or result.execution_no != run.execution_no):
            invalid()


def validate_result_link(repo, turn, generation_id, draft):
    result = repo.get_artifact_by_key(turn.session_id, turn.id, attempt_no=turn.attempt_no, key=RESULT_KEY)
    if result is None:
        invalid()
    row, details = typed_artifact(repo, turn, result.id, CastingResultDetails, RESULT_KEY, "result")
    if (row.parent_artifact_id != generation_id or details.generation_artifact_id != generation_id
            or details.route_artifact_id != draft.route_artifact_id):
        invalid()
    return row


def validate_snapshot_hash(row):
    from app.services.conversation_repository import fingerprint
    if row.content_fingerprint != fingerprint({"kind": "answer_draft", "version": 3, "payload": row.payload, "sources": []}):
        invalid()


def published_casting(repo, turn, message):
    from app.schemas.conversations import ConversationAnswer
    from app.schemas.rag import RagLlmInfo
    row = repo._snapshot(turn.session_id, turn.id, message.answer_snapshot_id)
    if row.kind != "answer_draft" or row.status != "available" or row.schema_version != 3:
        invalid()
    validate_snapshot_hash(row)
    draft = CastingAnswerDraft.model_validate_json(json.dumps(row.payload))
    generation, details = typed_artifact(repo, turn, row.artifact_id, CastingGenerationDetails, GENERATION_KEY, "generation")
    validate_source(repo, turn, draft, generation)
    validate_result_link(repo, turn, generation.id, draft)
    if message.content != draft.text:
        invalid()
    return ConversationAnswer(question=turn.question, answer=message.content, context_status="casting_design",
        citations=[], sources=[], llm=RagLlmInfo(provider=details.metrics.provider, model=details.metrics.model), casting=draft.casting)
