"""Synthetic external services; real guarded PostgreSQL, Hybrid, graph and snapshots."""
from uuid import UUID, uuid4

from sqlalchemy import select

from app.db.langgraph import CheckpointPool
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.rag.conversation_graph import ConversationGraph
from app.rag.query_rewrite import QueryRewriter
from app.services import hybrid_search
from app.services.document_qa_evidence_deletion import DocumentQaEvidenceDeletionService
from phase13_m2_support import FakeProvider, call, factory, riser_response, settings_for
from test_hybrid_search import FakeEmbeddingProvider, make_hit, make_response, make_source


EVIDENCE = "M3_SYNTHETIC_EVIDENCE 冒口用于补缩，应依据铸件热节确定。"
ANSWER = "M3_SYNTHETIC_ANSWER 根据当前补缩证据，需要核实铸件热节。[1]"


def source(engine, *, n=1, content=EVIDENCE, anchor="A"):
    did, cid = uuid4(), uuid4()
    metadata = {"kg_refs": [{"anchor_id": anchor, "graph_id": "G", "anchor_type": "table", "table_ref": "T-P8-1"}]}
    with factory(engine)() as db, db.begin():
        db.add(Document(id=did, original_filename=f"synthetic-{n}.md", object_key=f"synthetic/{did}"))
        db.flush()
        db.add(DocumentChunk(id=cid, document_id=did, chunk_index=n, content=content, source_metadata=metadata))
    return make_source(document_id=str(did), chunk_id=str(cid), chunk_index=n, content=content, source_metadata=metadata)


def redact(engine, did):
    # Synthetic PG fixtures ONLY; no object storage, Neo4j or OpenSearch deletion.
    with factory(engine)() as db, db.begin():
        document = db.scalar(select(Document).where(Document.id == UUID(str(did))).with_for_update())
        document.deletion_status = "deleting"
        db.flush()
        DocumentQaEvidenceDeletionService(db).redact(document.id)


class SearchHarness:
    def __init__(self, sources):
        self.sources, self.calls, self.search_calls = sources, [], []
        self.before, self.after, self.error = None, None, None
        self.embedding = FakeEmbeddingProvider()
        self.real_hybrid = hybrid_search.hybrid_search_chunks

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        if self.error:
            raise self.error
        return make_response(*(make_hit(row, 100.0 - i) for i, row in enumerate(self.sources)))

    def hybrid(self, db, **kwargs):
        self.calls.append(kwargs)
        if self.before:
            self.before()
        result = self.real_hybrid(db, **kwargs, embedding_provider=self.embedding, client=self)
        if self.after:
            self.after()
        return result


def install_search(monkeypatch, sources):
    harness = SearchHarness(sources)
    monkeypatch.setattr(hybrid_search, "hybrid_search_chunks", harness.hybrid)
    return harness


class ChatProvider(FakeProvider):
    def __init__(self, answer=ANSWER, rewrite=riser_response):
        self.answer, self.rewrite_calls, self.answer_calls = answer, [], []
        self.rewrite = rewrite
        super().__init__(self.respond)

    def respond(self, request):
        # Rewrite uses JSON envelopes; answer uses explicitly labelled sections.
        if request.messages[-1].content[0].text.startswith("{"):
            self.rewrite_calls.append(request)
            return self.rewrite(request)
        self.answer_calls.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer(request) if callable(self.answer) else self.answer


def runtime(engine, provider=None, graph_retrieval=None, **changes):
    config = dict(conversation_graph_version="phase13_m3_v2", reranker_enabled=False,
                  graph_retrieval_enabled=False, llm_provider="local", llm_model="synthetic",
                  reranker_provider="local_transformers", reranker_model="BAAI/bge-reranker-v2-m3",
                  reranker_batch_size=8, reranker_max_length=1024, reranker_timeout_seconds=2)
    config.update(changes)
    settings = settings_for(engine, **config)
    pool = CheckpointPool(settings)
    pool.open()
    provider = provider or ChatProvider()
    graph = ConversationGraph(factory(engine), pool, QueryRewriter(provider, settings), settings,
                              graph_retrieval=graph_retrieval)
    return graph, pool, provider


def result(graph, row):
    return graph.get_result(row.session_id, row.id, row.request_id, row.attempt_no)


def publish_staged(engine, graph, row):
    staged = result(graph, row)
    assert staged.checkpoint_complete
    call(engine, lambda r: r.transition_turn(row.session_id, row.id, expected_status="running",
         new_status="finalizing", expected_attempt=row.attempt_no))
    return call(engine, lambda r: r.publish_answer(row.session_id, row.id, staged.draft_snapshot_id,
                                                  expected_attempt=row.attempt_no))
