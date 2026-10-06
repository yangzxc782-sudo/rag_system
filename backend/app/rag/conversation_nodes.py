"""M3 nodes: bounded state references, replayable stages, no message publication."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import re
from time import perf_counter
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError

from app.core.errors import BusinessError
from app.rag.conversation_prompt import checked_request, prompt_cost, prompt_fingerprint
from app.rag.conversation_state import ExecutionIdentity, M3_GRAPH_VERSION
from app.rag.context_builder import RagContext, format_context_for_prompt
from app.rag.graph_context_builder import GraphContext, format_graph_context_for_prompt
from app.rag.history_budget import HistoryContext, read_rewrite_details, rehydrate_history
from app.rag.query_rewrite_prompt import current_rewrite_policy
from app.schemas.query_rewrite import REWRITE_STRATEGY
from app.rag.query_rewrite import clarification_text
from app.schemas.conversation_persistence import AnswerDraft, EvidenceSourceRef, PersistenceMetrics, SnapshotInput, TokenUsage
from app.schemas.conversation_rag import (
    CandidatePayload, CitationPayload, EvidenceDetails, GenerationDetails, GraphPayload,
    ResultDetails, RetrievalDetails, STAGE_ADAPTER, StagedConversationResult,
)
from app.services import rag
from app.services.chat_evidence import ChatEvidenceService, EvidencePlan, validate_graph_sources
from app.services.conversation_repository import ConversationError, ConversationRepository, fingerprint
from app.services.hybrid_search import HybridSearchResult


STAGE_KEYS = {name: f"chat_{name}:v1" for name in ("retrieval", "evidence", "generation", "result")}
STAGE_MODELS = {"retrieval": RetrievalDetails, "evidence": EvidenceDetails,
                "generation": GenerationDetails, "result": ResultDetails}


def stage_fingerprint(identity: ExecutionIdentity, kind: str, parent: UUID | None) -> str:
    return fingerprint({"request": identity.input_fingerprint, "request_id": str(identity.request_id),
                        "thread": str(identity.thread_id), "turn": str(identity.turn_id),
                        "attempt": identity.attempt_no, "generation": identity.attempt_no,
                        "graph": identity.graph_version, "rewrite_strategy": REWRITE_STRATEGY,
                        "stage": STAGE_KEYS[kind], "parent": str(parent) if parent else None})


def source_refs(chunk) -> tuple[EvidenceSourceRef, ...]:
    return (EvidenceSourceRef(UUID(chunk.document_id), UUID(chunk.chunk_id)),)


def graph_refs(evidence) -> tuple[EvidenceSourceRef, ...]:
    return tuple(sorted({EvidenceSourceRef(UUID(p.document_id), UUID(p.chunk_id)) for p in evidence.provenance},
                        key=lambda r: (str(r.document_id), str(r.chunk_id))))


class ConversationRagNodes:
    def __init__(self, context_nodes, *, graph_retrieval=None):
        self.base = context_nodes
        self.session_factory, self.settings = context_nodes.session_factory, context_nodes.settings
        self.provider = context_nodes.rewriter.provider
        self.evidence_service = ChatEvidenceService(self.session_factory, self.settings, graph_retrieval=graph_retrieval)
        self.after_stage_commit = None  # Fault-injection seam, never correctness state.

    def node(self, name: str):
        method = getattr(self, name)
        def run(state, config: RunnableConfig):
            try:
                return method(state, config)
            except BusinessError as exc:
                code = exc.code if re.fullmatch(r"[A-Z0-9_]{1,100}", exc.code) else "QA_STAGE_FAILED"
            except (ValidationError, ValueError, TypeError):
                code = "QA_STAGE_INVALID"
            return {"stage": "failed", "terminal_status": "needs_recovery", "outcome": None, "error_code": code}
        return run

    @contextmanager
    def _read(self, state, config):
        identity = self.base._identity(state, config)
        if state["state_schema_version"] not in {2, 3} or state["evidence_generation"] != identity.attempt_no:
            raise ConversationError("QA_EXECUTION_STALE", "Invalid M3 evidence generation.", status_code=409)
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            turn = identity.validate(repo)
            yield repo, identity, turn

    def _rewrite(self, repo, identity, turn, state):
        artifact = repo.get_artifact(identity.thread_id, identity.turn_id, UUID(state["rewrite_artifact_id"]))
        return artifact, self.base._reuse(repo, identity, turn, artifact)

    @staticmethod
    def _existing(repo, identity, kind, parent):
        artifact = repo.get_artifact_by_key(identity.thread_id, identity.turn_id,
                                           attempt_no=identity.attempt_no, key=STAGE_KEYS[kind])
        if artifact is None:
            return None
        if (artifact.kind != kind or artifact.schema_version != 1 or artifact.parent_artifact_id != parent
                or artifact.input_fingerprint != stage_fingerprint(identity, kind, parent)):
            raise ConversationError("QA_STAGE_INVALID", "Stage identity does not match current execution.", status_code=409)
        details = STAGE_ADAPTER.validate_json(json.dumps(artifact.details))
        if not isinstance(details, STAGE_MODELS[kind]):
            raise ConversationError("QA_STAGE_INVALID", "Stage metadata type mismatch.", status_code=409)
        return artifact, details

    @staticmethod
    def _view(repo, identity, artifact_id, key, kind):
        view = repo.get_snapshot_by_key(identity.thread_id, identity.turn_id, artifact_id, key)
        if view.kind != kind or view.status != "available" or view.payload is None:
            raise ConversationError("QA_EVIDENCE_UNAVAILABLE", "Stage evidence is no longer available; use a new attempt.", status_code=409)
        return view

    @staticmethod
    def _require_sources(view, refs):
        if set(view.sources) != set(refs):
            raise ConversationError("QA_SOURCE_INVALID", "Snapshot provenance does not match its payload.", status_code=409)

    def _retrieval(self, repo, identity, saved):
        artifact, details = saved
        if details.evidence_generation != identity.attempt_no:
            raise ConversationError("QA_EXECUTION_STALE", "Retrieval evidence generation mismatch.", status_code=409)
        items = []
        for key in details.snapshot_keys:
            view = self._view(repo, identity, artifact.id, key, "candidate")
            item = CandidatePayload.model_validate_json(json.dumps(view.payload)).item
            self._require_sources(view, source_refs(item))
            items.append(item)
        return HybridSearchResult(details.query, details.limit, len(items), items)

    def _evidence(self, repo, identity, turn, saved, query, pending=None):
        artifact, details = saved
        if details.evidence_generation != identity.attempt_no:
            raise ConversationError("QA_EXECUTION_STALE", "Evidence generation mismatch.", status_code=409)
        chunks, units, refs = [], [], set()
        for key in details.citation_keys:
            view = self._view(repo, identity, artifact.id, key, "citation")
            chunk = CitationPayload.model_validate_json(json.dumps(view.payload)).chunk
            self._require_sources(view, source_refs(chunk))
            chunks.append(chunk)
            refs.update(view.sources)
        if [c.citation_id for c in chunks] != list(range(1, len(chunks) + 1)):
            raise ConversationError("QA_STAGE_INVALID", "Citation order is inconsistent.", status_code=409)
        context = RagContext(query, "ok" if chunks else "no_context", chunks, self.settings.rag_context_max_chars, 0)
        context = replace(context, total_chars=len(format_context_for_prompt(context)))
        for key in details.graph_keys:
            view = self._view(repo, identity, artifact.id, key, "graph")
            unit = GraphPayload.model_validate_json(json.dumps(view.payload)).evidence
            self._require_sources(view, graph_refs(unit))
            units.append(unit)
            refs.update(view.sources)
        graph = GraphContext(enabled=details.graph_enabled, status="ok" if units else ("empty" if details.graph_enabled else "disabled"),
            evidence=tuple(units), was_truncated=details.graph_truncated, max_chars=self.settings.rag_graph_context_max_chars)
        graph = replace(graph, total_chars=len(format_graph_context_for_prompt(graph)))
        if validate_graph_sources(graph, context).evidence != graph.evidence:
            raise ConversationError("QA_SOURCE_INVALID", "Graph evidence does not match current citations.", status_code=409)
        history = rehydrate_history(repo, turn, details.history_message_ids, pending, self.settings)
        plan = EvidencePlan(context, graph, history, details.graph_triggered)
        request = checked_request(turn.question, query, history, context, graph, self.settings)
        if prompt_fingerprint(request.messages) != details.prompt_fingerprint:
            raise ConversationError("QA_PROMPT_CHANGED", "Saved evidence no longer yields the original prompt.", status_code=409)
        return plan, tuple(refs), request

    def _save(self, identity, kind, parent, details, snapshots=(), *, retrieval_log=None):
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            identity.validate(repo)  # Read only; Document locks are acquired first below.
            artifact = repo.save_stage(identity.thread_id, identity.turn_id, expected_attempt=identity.attempt_no,
                key=STAGE_KEYS[kind], kind=kind, input_fingerprint=stage_fingerprint(identity, kind, parent),
                details=details, snapshots=tuple(snapshots), parent_artifact_id=parent)
            if retrieval_log is not None:
                repo.record_retrieval(identity.thread_id, identity.turn_id, retrieval_log,
                    expected_attempt=identity.attempt_no, evidence_generation=identity.attempt_no,
                    query=details.query, top_k=details.limit, metrics=PersistenceMetrics(
                        candidate_count=details.hybrid_count, rerank_applied=details.rerank_applied,
                        fallback_code=details.fallback_reason, latency_ms=details.retrieval_ms + details.rerank_ms))
            ident = artifact.id
        if self.after_stage_commit is not None:
            self.after_stage_commit(kind)
        return ident

    def retrieve_and_rerank(self, state, config):
        with self._read(state, config) as (repo, identity, turn):
            rewrite, details = self._rewrite(repo, identity, turn, state)
            if details.result.decision not in {"standalone", "rewritten"}:
                raise ConversationError("QA_STAGE_INVALID", "Clarification cannot enter retrieval.", status_code=409)
            saved = self._existing(repo, identity, "retrieval", rewrite.id)
            if saved:
                self._retrieval(repo, identity, saved)
                return {"retrieval_artifact_id": str(saved[0].id), "stage": "retrieved"}
            query, limit, document_id, parent = details.result.standalone_query, turn.retrieval_limit, turn.document_id, rewrite.id
        stage = self.evidence_service.retrieve(query, limit, document_id)
        snapshots = [SnapshotInput(f"candidate_{i:03d}", "candidate", CandidatePayload(item=item).model_dump(mode="json"), source_refs(item))
                     for i, item in enumerate(stage.outcome.search_result.items)]
        details = RetrievalDetails(query=query, limit=limit, document_id=document_id, evidence_generation=identity.attempt_no,
            hybrid_limit=stage.hybrid_limit, candidate_limit=stage.candidate_limit, hybrid_count=stage.hybrid_count,
            rerank_applied=stage.outcome.applied, fallback_reason=stage.outcome.fallback_reason,
            retrieval_ms=stage.retrieval_ms, rerank_ms=stage.rerank_ms, snapshot_keys=[s.key for s in snapshots])
        ident = self._save(identity, "retrieval", parent, details, snapshots, retrieval_log=UUID(state["current_message_id"]))
        return {"retrieval_artifact_id": str(ident), "stage": "retrieved"}

    def build_evidence(self, state, config):
        with self._read(state, config) as (repo, identity, turn):
            rewrite, rewritten = self._rewrite(repo, identity, turn, state)
            retrieval = self._existing(repo, identity, "retrieval", rewrite.id)
            if retrieval is None or str(retrieval[0].id) != state["retrieval_artifact_id"]:
                raise ConversationError("QA_STAGE_INVALID", "Missing current retrieval artifact.", status_code=409)
            query = rewritten.result.standalone_query
            existing = self._existing(repo, identity, "evidence", retrieval[0].id)
            if existing:
                plan, _, _ = self._evidence(repo, identity, turn, existing, query, rewritten.pending_clarification_turn_id)
                return {"evidence_artifact_id": str(existing[0].id), "stage": "evidence_built",
                        "outcome": "answer" if plan.context.chunks else "no_context"}
            search = self._retrieval(repo, identity, retrieval)
            history = (HistoryContext() if rewritten.result.decision == "standalone" or rewritten.result.history_scope == "none"
                       else rehydrate_history(repo, turn, rewritten.history_message_ids, rewritten.pending_clarification_turn_id, self.settings))
            question, parent, preserve = turn.question, retrieval[0].id, retrieval[1].rerank_applied
        plan = self.evidence_service.build(question, query, history, search, preserve_order=preserve)
        request = checked_request(question, query, plan.history, plan.context, plan.graph, self.settings)
        citations = [SnapshotInput(f"citation_{i:03d}", "citation", CitationPayload(chunk=c).model_dump(mode="json"), source_refs(c))
                     for i, c in enumerate(plan.context.chunks)]
        graphs = [SnapshotInput(f"graph_{i:03d}", "graph", GraphPayload(evidence=g).model_dump(mode="json"), graph_refs(g))
                  for i, g in enumerate(plan.graph.evidence)]
        details = EvidenceDetails(evidence_generation=identity.attempt_no, citation_keys=[s.key for s in citations], graph_keys=[s.key for s in graphs],
            history_message_ids=plan.history.message_ids, graph_enabled=plan.graph.enabled, graph_triggered=plan.graph_triggered,
            graph_truncated=plan.graph.was_truncated, input_estimated_tokens=prompt_cost(request.messages),
            prompt_fingerprint=prompt_fingerprint(request.messages))
        ident = self._save(identity, "evidence", parent, details, [*citations, *graphs])
        return {"evidence_artifact_id": str(ident), "stage": "evidence_built", "outcome": "answer" if citations else "no_context"}

    def _load_plan(self, repo, identity, turn, state):
        rewrite, details = self._rewrite(repo, identity, turn, state)
        retrieval = self._existing(repo, identity, "retrieval", rewrite.id)
        if retrieval is None or str(retrieval[0].id) != state["retrieval_artifact_id"]:
            raise ConversationError("QA_STAGE_INVALID", "Retrieval ancestry mismatch.", status_code=409)
        saved = self._existing(repo, identity, "evidence", retrieval[0].id)
        if saved is None or str(saved[0].id) != state["evidence_artifact_id"]:
            raise ConversationError("QA_STAGE_INVALID", "Missing current evidence artifact.", status_code=409)
        return self._evidence(repo, identity, turn, saved, details.result.standalone_query, details.pending_clarification_turn_id)

    def _draft(self, repo, identity, generation):
        artifact, details = generation
        view = self._view(repo, identity, artifact.id, "answer", "answer_draft")
        draft = AnswerDraft.model_validate(view.payload)
        if draft.outcome != details.outcome or (draft.outcome == "answer") != bool(view.sources):
            raise ConversationError("QA_STAGE_INVALID", "Draft outcome/provenance mismatch.", status_code=409)
        return view, draft

    def generate_answer(self, state, config):
        parent = UUID(state["evidence_artifact_id"])
        with self._read(state, config) as (repo, identity, turn):
            plan, refs, request = self._load_plan(repo, identity, turn, state)
            saved = self._existing(repo, identity, "generation", parent)
            if saved:
                view, _ = self._draft(repo, identity, saved)
                self._require_sources(view, refs)
                return {"generation_artifact_id": str(saved[0].id), "stage": "generated"}
            if not plan.context.chunks:
                raise ConversationError("QA_STAGE_INVALID", "No evidence for answer generation.", status_code=409)
        started = perf_counter()
        generated = rag.generate_request(request, self.provider)
        metrics = PersistenceMetrics(provider=generated.provider, model=generated.model,
            latency_ms=int((perf_counter() - started) * 1000), usage=TokenUsage(
                input_tokens=generated.usage.prompt_tokens, output_tokens=generated.usage.completion_tokens,
                total_tokens=generated.usage.total_tokens) if generated.usage else None)
        details = GenerationDetails(outcome="answer", snapshot_keys=["answer"], metrics=metrics)
        ident = self._save(identity, "generation", parent, details,
            [SnapshotInput("answer", "answer_draft", {"text": generated.text.strip(), "outcome": "answer"}, refs)])
        return {"generation_artifact_id": str(ident), "stage": "generated"}

    def stage_clarification(self, state, config):
        with self._read(state, config) as (repo, identity, turn):
            rewrite, details = self._rewrite(repo, identity, turn, state)
            if details.result.decision != "clarify":
                raise ConversationError("QA_STAGE_INVALID", "Not a clarification decision.", status_code=409)
            text = clarification_text(details.result)
        return self._stage_result(state, config, outcome="clarification", text=text)

    def stage_result(self, state, config):
        return self._stage_result(state, config, outcome=state["outcome"], text=self.settings.rag_no_context_message)

    def _stage_result(self, state, config, *, outcome, text):
        parent = UUID(state["rewrite_artifact_id"] if outcome == "clarification" else state["evidence_artifact_id"])
        with self._read(state, config) as (repo, identity, turn):
            refs = ()
            if outcome != "clarification":
                plan, refs, _ = self._load_plan(repo, identity, turn, state)
                if bool(plan.context.chunks) != (outcome == "answer"):
                    raise ConversationError("QA_STAGE_INVALID", "Result outcome does not match evidence.", status_code=409)
            generation = self._existing(repo, identity, "generation", parent)
            if generation:
                view, draft = self._draft(repo, identity, generation)
                self._require_sources(view, refs)
                if draft.outcome != outcome:
                    raise ConversationError("QA_STAGE_INVALID", "Result differs from saved draft.", status_code=409)
                generation_id = generation[0].id
            elif outcome == "answer":
                raise ConversationError("QA_STAGE_INVALID", "Answer has no saved generation.", status_code=409)
            else:
                generation_id = None
        if generation_id is None:
            generation_id = self._save(identity, "generation", parent,
                GenerationDetails(outcome=outcome, snapshot_keys=["answer"]),
                [SnapshotInput("answer", "answer_draft", {"text": text, "outcome": outcome})])
        # Result is a small immutable reference. Publication still rechecks the
        # draft under Document locks; a checkpoint alone never makes it published.
        with self._read(state, config) as (repo, identity, turn):
            self._draft(repo, identity, self._existing(repo, identity, "generation", parent))
            result = self._existing(repo, identity, "result", generation_id)
            result_id = result[0].id if result else None
        if result_id is None:
            result_id = self._save(identity, "result", generation_id, ResultDetails(outcome=outcome,
                generation_artifact_id=generation_id, evidence_artifact_id=None if outcome == "clarification" else parent))
        return {"generation_artifact_id": str(generation_id), "result_artifact_id": str(result_id),
                "stage": "clarification_staged" if outcome == "clarification" else "result_staged",
                "terminal_status": "result_staged", "outcome": outcome, "error_code": None,
                "pending_clarification_turn_id": (state["pending_clarification_turn_id"] or state["turn_id"])
                    if outcome == "clarification" else None}

    def read_result(self, sid: UUID, tid: UUID, request_id: UUID, attempt: int) -> StagedConversationResult:
        with self.session_factory() as db, db.begin():
            repo = ConversationRepository(db)
            turn = repo.get_turn(sid, tid)
            if turn.request_id != request_id or turn.attempt_no != attempt:
                raise ConversationError("QA_EXECUTION_STALE", "Result execution identity mismatch.", status_code=409)
            version = (turn.graph_version or M3_GRAPH_VERSION) if self.settings.casting_design_enabled else M3_GRAPH_VERSION
            identity = ExecutionIdentity(sid, tid, request_id, attempt, turn.request_fingerprint, version)
            repo.require_rewrite_policy(turn, current_rewrite_policy())
            artifact = repo.get_artifact_by_key(sid, tid, attempt_no=attempt, key=STAGE_KEYS["result"])
            if artifact is None:
                raise ConversationError("QA_RESULT_NOT_STAGED", "No staged result is available.", status_code=409)
            _, result = self._existing(repo, identity, "result", artifact.parent_artifact_id)
            if result.generation_artifact_id != artifact.parent_artifact_id:
                raise ConversationError("QA_STAGE_INVALID", "Result parent mismatch.", status_code=409)
            generation = repo.get_artifact(sid, tid, result.generation_artifact_id)
            saved_generation = self._existing(repo, identity, "generation", generation.parent_artifact_id)
            if saved_generation is None or saved_generation[0].id != generation.id:
                raise ConversationError("QA_STAGE_INVALID", "Result generation mismatch.", status_code=409)
            view, draft = self._draft(repo, identity, saved_generation)
            if result.outcome != draft.outcome:
                raise ConversationError("QA_STAGE_INVALID", "Result outcome mismatch.", status_code=409)
            context, graph = RagContext(turn.question, "no_context", [], 0, 0), GraphContext(enabled=False, status="disabled")
            if result.evidence_artifact_id:
                evidence = repo.get_artifact(sid, tid, result.evidence_artifact_id)
                saved_evidence = self._existing(repo, identity, "evidence", evidence.parent_artifact_id)
                retrieval = repo.get_artifact(sid, tid, evidence.parent_artifact_id)
                saved_retrieval = self._existing(repo, identity, "retrieval", retrieval.parent_artifact_id)
                rewrite = repo.get_artifact(sid, tid, retrieval.parent_artifact_id)
                if (generation.parent_artifact_id != evidence.id or saved_evidence is None
                        or saved_evidence[0].id != evidence.id or saved_retrieval is None
                        or saved_retrieval[0].id != retrieval.id or result.outcome == "clarification"):
                    raise ConversationError("QA_STAGE_INVALID", "Result evidence ancestry mismatch.", status_code=409)
                self.base._reuse(repo, identity, turn, rewrite)
                pending = read_rewrite_details(rewrite).pending_clarification_turn_id
                plan, refs, _ = self._evidence(repo, identity, turn, saved_evidence, saved_retrieval[1].query, pending)
                self._require_sources(view, refs)
                context, graph = plan.context, plan.graph
            elif result.outcome != "clarification":
                raise ConversationError("QA_STAGE_INVALID", "Result evidence reference missing.", status_code=409)
            else:
                rewrite = repo.get_artifact(sid, tid, generation.parent_artifact_id)
                if self.base._reuse(repo, identity, turn, rewrite).result.decision != "clarify":
                    raise ConversationError("QA_STAGE_INVALID", "Clarification ancestry mismatch.", status_code=409)
            return StagedConversationResult(sid, tid, attempt, artifact.id, view.id, turn.question, draft.text,
                draft.outcome, tuple(rag.build_citations(context)), graph,
                saved_generation[1].metrics.provider, saved_generation[1].metrics.model)
