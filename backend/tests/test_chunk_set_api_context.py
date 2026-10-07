"""HTTP contracts and version provenance across RRF/BGE/Phase13 payloads."""
from dataclasses import replace
from uuid import uuid4
import pytest

from app.api.v1 import documents as api
from app.core.errors import BusinessError
from app.ingestion.sequential_chunker import SegmentationConfig
from app.rag.context_builder import build_rag_context, format_context_for_prompt
from app.schemas.conversation_rag import CandidatePayload, CitationPayload
from app.services import rag, reranking
from app.services.hybrid_search import HybridSearchResult
from test_kg_v2_api import client
from test_rag_reranking import items, settings, FakeRerankingService


def status(doc, sid):
    return dict(chunk_set_id=sid, document_id=doc, source_version=uuid4(), graph_build_id=uuid4(), request_id=uuid4(),
        operation="process", status="chunks_ready", job_status="queued", stage="chunks_ready", chunk_count=3,
        embedding_counts={"not_started": 3}, segmentation_config=SegmentationConfig().model_dump(), is_current=False,
        publication_revision=0, index_name=None, last_error_code=None, lease_expires_at=None, can_advance=True, search_enabled=False)


def test_api_create_and_advance_are_independent(client, monkeypatch):
    from app.core.config import Settings
    client.app.state.settings = Settings(_env_file=None)
    doc, sid = uuid4(), uuid4()
    calls = []
    def prepare(db, did, source, graph, request, config, *, operation, settings):
        calls.append((did, source, graph, request, config, operation))
        return status(doc, sid)
    monkeypatch.setattr(api, "prepare_chunk_set", prepare)
    def advance(db, did, set_id, *, retry):
        calls.append((did, set_id, retry))
        return status(doc, sid)
    monkeypatch.setattr(api, "advance_chunk_set", advance)
    body = dict(source_version=str(uuid4()), graph_build_id=str(uuid4()), request_id=str(uuid4()), operation="rechunk",
                config={"chunk_size": 80, "overlap": 10, "boundary": "line"})
    path = f"/api/v1/documents/{doc}/chunk-sets"
    result = client.post(path, json=body)
    assert result.status_code == 200 and len(calls) == 1 and calls[0][-1] == "rechunk"
    assert result.json()["data"]["chunk_set_id"] == str(sid)
    assert client.post(f"{path}/{sid}/advance", json={"retry": True}).status_code == 200
    assert calls[-1] == (doc, sid, True) and len(calls) == 2


@pytest.mark.parametrize("config", [{"chunk_size": 10, "overlap": 10}, {"chunk_size": True},
                                  {"chunk_size": 20, "overlap": 0, "boundary": "semantic_rewrite"},
                                  {"chunk_size": 20, "overlap": 0, "source_spans": []}])
def test_api_rejects_invalid_segmentation_before_service(client, config):
    result = client.post(f"/api/v1/documents/{uuid4()}/chunk-sets", json=dict(source_version=str(uuid4()), graph_build_id=str(uuid4()),
        request_id=str(uuid4()), config=config))
    assert result.status_code == 422


def test_disabled_error_and_strict_retry(client, monkeypatch):
    def disabled(*args, **kwargs):
        raise BusinessError("CHUNK_BUILD_DISABLED", "disabled", status_code=503)
    monkeypatch.setattr(api, "advance_chunk_set", disabled)
    path = f"/api/v1/documents/{uuid4()}/chunk-sets/{uuid4()}/advance"
    assert client.post(path, json={"retry": "true"}).status_code == 422
    assert client.post(path, json={}).status_code == 503


def test_versions_and_refs_survive_bge_and_phase13_serialization(monkeypatch):
    config = settings(reranker_candidate_limit=4)
    refs = [{"anchor_type": "clause", "graph_id": "KG-g", "anchor_id": "KG-g::C-1", "clause_ref": "C-1"}]
    rows = [replace(item, source_metadata={"kg_refs": refs}, source_version=str(uuid4()), graph_build_id=str(uuid4()),
        chunk_set_id=str(uuid4()), source_start=20, source_end=20 + len(item.content), content_sha256="a" * 64,
        embedding_fingerprint="b" * 64) for item in items(4)]
    reranker = FakeRerankingService()
    monkeypatch.setattr(reranking, "get_reranking_service", lambda _: reranker)
    outcome = rag.optional_rerank_chunks("query", HybridSearchResult("query", 4, 4, rows), config, limit=2)
    assert outcome.applied and outcome.search_result.items == rows[::-1][:2]
    assert [c.content for c in reranker.calls[0].candidates] == [r.content for r in rows]
    context = build_rag_context("query", outcome.search_result, config, preserve_order=True)
    for item, chunk in zip(outcome.search_result.items, context.chunks, strict=True):
        assert CandidatePayload.model_validate_json(CandidatePayload(item=item).model_dump_json()).item == item
        assert CitationPayload.model_validate_json(CitationPayload(chunk=chunk).model_dump_json()).chunk == chunk
        assert chunk.chunk_set_id == item.chunk_set_id and chunk.source_metadata == item.source_metadata
    assert "KG-g" not in format_context_for_prompt(context)
    # M4 cannot trust copied metadata without an authoritative SQL binding.
    graph = rag.build_graph_context_for_rag(context, settings(graph_retrieval_enabled=True, pdf_kg_search_enabled=True), None)
    assert not graph.evidence and graph.source_error == "authority_unavailable"
