"""Read committed answers and currently visible snapshots, never re-run RAG."""
import base64
from datetime import datetime
import json
from uuid import UUID

from app.rag.graph_context_builder import GraphContext
from app.rag.graph_response import build_graph_response
from app.schemas.conversation_rag import CitationPayload, EvidenceDetails, GenerationDetails, GraphPayload, ResultDetails
from app.schemas.conversations import ConversationAnswer, EvidenceSourceStatus, SessionCreateResponse
from app.schemas.rag import RagCitationItem, RagLlmInfo
from app.services.conversation_repository import ConversationError


def session_view(row):
    return SessionCreateResponse(thread_id=row.id, title=row.title or "新会话",
                                 created_at=row.created_at, updated_at=row.updated_at)


def encode_cursor(row):
    return base64.urlsafe_b64encode(json.dumps([row.updated_at.isoformat(), str(row.id)]).encode()).decode()


def decode_cursor(value):
    if value is None:
        return None
    try:
        if len(value) > 256:
            raise ValueError()
        stamp, ident = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        stamp = datetime.fromisoformat(stamp)
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp, UUID(ident)
    except (TypeError, ValueError, UnicodeError):
        raise ConversationError("QA_PAGE_INVALID", "Invalid session cursor.", status_code=422) from None


def _details(repo, turn, ident, model, kind):
    row = repo.get_artifact(turn.session_id, turn.id, ident)
    if row.attempt_no != turn.attempt_no or row.kind != kind or row.schema_version != 1:
        raise ConversationError("QA_RESULT_INVALID", "Stored result identity is invalid.", status_code=409)
    return model.model_validate_json(json.dumps(row.details))


def published_answer(repo, turn):
    """Use the durable assistant body even when the recovery draft was redacted."""
    message = repo.get_answer(turn.session_id, turn.id)
    if turn.status != "completed" or message is None:
        raise ConversationError("QA_RESULT_NOT_PUBLISHED", "Answer has not been committed.", status_code=409)
    if turn.outcome == "casting_design":
        from app.services.casting_provenance import published_casting
        return published_casting(repo, turn, message)
    result = ConversationAnswer(question=turn.question, answer=message.content,
        context_status="ok" if turn.outcome == "answer" else turn.outcome,
        citations=[], sources=[], llm=RagLlmInfo())
    artifact = repo.get_artifact_by_key(turn.session_id, turn.id, attempt_no=turn.attempt_no, key="chat_result:v1")
    if artifact is None:
        return result  # M1/legacy published business history has no M3 detail record.
    details = _details(repo, turn, artifact.id, ResultDetails, "result")
    generation = _details(repo, turn, details.generation_artifact_id, GenerationDetails, "generation")
    draft = repo.get_snapshot_by_key(turn.session_id, turn.id, details.generation_artifact_id, "answer")
    if (draft.id != message.answer_snapshot_id or details.outcome != turn.outcome
            or generation.outcome != turn.outcome):
        raise ConversationError("QA_RESULT_INVALID", "Published answer identity is invalid.", status_code=409)
    result.llm = RagLlmInfo(provider=generation.metrics.provider, model=generation.metrics.model)
    if details.evidence_artifact_id is None:
        return result
    evidence = _details(repo, turn, details.evidence_artifact_id, EvidenceDetails, "evidence")
    graphs = []
    for kind, keys in (("citation", evidence.citation_keys), ("graph", evidence.graph_keys)):
        for index, key in enumerate(keys, 1):
            view = repo.get_snapshot_by_key(turn.session_id, turn.id, details.evidence_artifact_id, key)
            if view.kind != kind:
                raise ConversationError("QA_RESULT_INVALID", "Snapshot type mismatch.", status_code=409)
            result.sources.append(EvidenceSourceStatus(snapshot_id=view.id, kind=kind,
                citation_id=index if kind == "citation" else None,
                document_ids=sorted({s.document_id for s in view.sources}),
                chunk_ids=sorted({s.chunk_id for s in view.sources if s.chunk_id}), status=view.status))
            if view.payload is None:
                continue
            from app.rag.conversation_nodes import source_refs, graph_refs
            if kind == "citation":
                chunk = CitationPayload.model_validate_json(json.dumps(view.payload)).chunk
                if chunk.citation_id != index or set(source_refs(chunk)) != set(view.sources):
                    raise ConversationError("QA_RESULT_INVALID", "Citation provenance mismatch.", status_code=409)
                result.citations.append(RagCitationItem.from_service_citation(chunk))
            else:
                graph = GraphPayload.model_validate_json(json.dumps(view.payload)).evidence
                if set(graph_refs(graph)) != set(view.sources):
                    raise ConversationError("QA_RESULT_INVALID", "Graph provenance mismatch.", status_code=409)
                graphs.append(graph)
    # A multi-source graph is an indivisible unit; also suppress it if any of its
    # prompt citations is no longer visible. Never re-number historical citations.
    visible = {c.citation_id for c in result.citations}
    graphs = [g for g in graphs if set(g.source_citations) <= visible]
    result.graph = build_graph_response(GraphContext(enabled=evidence.graph_enabled,
        status="ok" if graphs else "empty", evidence=tuple(graphs), was_truncated=evidence.graph_truncated),
        triggered=evidence.graph_triggered)
    if result.graph and graphs and len(graphs) != len(evidence.graph_keys):
        result.graph.status = "partial"
    return result
