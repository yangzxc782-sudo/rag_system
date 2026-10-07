"""M4 contracts: local ORM + JSON wire fake, never a live PG/OpenSearch test.

Reuse M3's isolated SQLite fixture for Writer/service persistence. PostgreSQL
JSONB codec coverage is separate; actual server roundtrips remain an M7 gate.
The search fake projects requested _source fields from the actual bulk payload,
so an omitted field cannot be hidden by a handcrafted search response.
"""

from __future__ import annotations

import builtins
from copy import deepcopy
from dataclasses import replace
import json
import socket
from types import SimpleNamespace

import pytest
from opensearchpy.serializer import JSONSerializer
from sqlalchemy.dialects.postgresql import JSONB, dialect
from sqlalchemy.orm import Session

from pdf_source_fixtures import pdf_draft
from app.models.document_chunk import DocumentChunk
from app.rag import context_builder
from app.schemas.rag import RagAskData, RagAskRequest, RagCitationItem
from app.schemas.search import SearchData, SearchItem, SearchRequest
from app.search_engine.index_schema import build_casting_chunks_index_body
from app.services import embeddings, hybrid_search, rag, search_index
from app.services.document_chunk_writer import DocumentChunkWriter
from app.services.document_operation_guard import DocumentOperationGuard
from test_document_chunk_writer import db, document  # isolated ORM fixtures
from test_document_embeddings import FakeProvider
from test_rag_service import FakeLLMProvider
from test_search_index import FakeIndices


TABLE_REF = {
    "anchor_id": "KG-20260826-001::T-P8-1",
    "graph_id": "KG-20260826-001",
    "anchor_type": "table",
    "table_ref": "T-P8-1",
}
CLAUSE_REF = {
    "anchor_id": "KG-20260826-001::SEC-8",
    "graph_id": "KG-20260826-001",
    "anchor_type": "clause",
    "clause_ref": "C0001",
}
CONTENT = "| Si | 温度 | 壁厚 |\n| --- | --- | --- |\n| ≤7.50% | ≥720℃ | 3–5 mm |\n"
CONTROL_TEXT = ("kg-anchor-start", "kg-anchor-end", "anchor_id:",
                "graph_id:", "anchor_type:", "table_ref:")


def provenance(refs):
    return {
        "parser_provider": "mineru_api",
        "parser_version": "pdf-fixture-v1",
        "chunk_method": "pdf_fixture",
        "content_format": "markdown",
        "section_path": ["第8章", "化学成分"],
        "kg_refs": deepcopy(refs),
        "other_provenance": {"nullable": None, "values": ["中文", 0, False]},
    }


METADATA_CASES = [
    pytest.param(None, id="null-metadata"),
    pytest.param({}, id="empty-metadata"),
    pytest.param(provenance([]), id="empty-refs"),
    pytest.param(provenance([TABLE_REF]), id="one-ref"),
    # Deliberately not alphabetical anchor order: serialization must not sort it.
    pytest.param(provenance([TABLE_REF, CLAUSE_REF]), id="many-refs"),
    pytest.param(provenance([CLAUSE_REF]), id="null-table-ref"),
    pytest.param({"parser_provider": "mineru", "block_ids": ["old-block"],
                  "page_start": 8}, id="legacy-no-refs"),
]


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("M4 contracts must not connect to external services")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    original_import = builtins.__import__

    def without_neo4j(name, *args, **kwargs):
        if name == "neo4j" or name.startswith("neo4j."):
            pytest.fail("M4 must not load or invoke Neo4j")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_neo4j)


@pytest.fixture
def settings():
    return SimpleNamespace(
        search_index_name="m4-contract-chunks-v1",
        search_index_alias="m4-contract-chunks-current",
        search_index_batch_size=2,
        search_vector_space="cosine",
        search_content_analyzer="ik_max_word", search_query_analyzer="ik_smart",
        embedding_model="fake-qwen", embedding_dim=1024, embedding_batch_size=2,
        hybrid_keyword_weight=0.5, hybrid_vector_weight=0.5, hybrid_rrf_k=60,
        hybrid_keyword_top_k=50, hybrid_vector_top_k=50,
        rag_top_k=10, rag_context_max_chars=12000,
        rag_no_context_message="No sufficient context.", reranker_enabled=False,
    )


class WireSearchClient:
    """In-memory transport only; does not emulate OpenSearch indexing semantics."""

    def __init__(self, settings, channel="both"):
        self.settings = settings
        self.channel = channel
        self.indices = FakeIndices()
        self.sources = {}
        self.search_calls = []
        self.bulk_calls = []
        self.deleted = []
        self.serializer = JSONSerializer()

    def bulk(self, *, body, refresh):
        assert refresh is True
        wire = [self.serializer.loads(self.serializer.dumps(row)) for row in body]
        self.bulk_calls.append(wire)
        items = []
        for action, source in zip(wire[::2], wire[1::2], strict=True):
            assert action["index"]["_index"] == self.settings.search_index_name
            chunk_id = action["index"]["_id"]
            assert source["chunk_id"] == chunk_id
            self.sources[chunk_id] = source
            items.append({"index": {"_id": chunk_id, "status": 201}})
        return {"errors": False, "items": items}

    def delete_by_query(self, *, index, body, **kwargs):
        assert index == self.settings.search_index_name
        document_id = body["query"]["term"]["document_id"]
        removed = [key for key, source in self.sources.items()
                   if source["document_id"] == document_id]
        for key in removed:
            del self.sources[key]
        self.deleted.append(removed)
        return {"deleted": len(removed)}

    def search(self, *, index, body):
        assert index == self.settings.search_index_alias
        self.search_calls.append(deepcopy(body))
        channel = "vector" if "knn" in body["query"] else "keyword"
        if self.channel not in {"both", channel}:
            return {"hits": {"hits": []}}
        hits = [{
            "_id": chunk_id, "_score": 1.0,
            "_source": {key: deepcopy(source[key]) for key in body["_source"] if key in source},
        } for chunk_id, source in self.sources.items()]
        return self.serializer.loads(self.serializer.dumps({"hits": {"hits": hits}}))


def persist(db, document, metadata):
    draft = pdf_draft(CONTENT)
    draft = replace(draft, source_metadata=deepcopy(metadata or {}))
    DocumentOperationGuard(db).lock_normal(document.id)
    chunk, = DocumentChunkWriter(db).write(document_id=document.id, drafts=[draft])
    if metadata is None:
        # Model a legacy nullable row, not a new nullable ChunkDraft contract.
        chunk.source_metadata = None
    document.process_status = "parsed"
    chunk_id = chunk.id
    db.commit()
    db.expire_all()
    return db.get(DocumentChunk, chunk_id)


def encode_and_index(db, document, settings, monkeypatch, client):
    provider = FakeProvider()
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embeddings, "get_embedding_provider", lambda _: provider)
    embedded = embeddings.generate_document_embeddings(db, document.id)
    indexed = search_index.rebuild_search_index(
        db, scope="document", document_id=document.id, settings=settings, client=client,
    )
    assert indexed.indexed == embedded.embedded
    return provider


def retrieve(db, settings, client, provider):
    return hybrid_search.hybrid_search_chunks(
        db, query="Si 温度 壁厚", limit=10, settings=settings, client=client,
        embedding_provider=provider,
        deletion_filter_session_factory=lambda: Session(db.get_bind()),
    )


def assert_metadata(actual, expected):
    assert actual == expected
    refs = (actual or {}).get("kg_refs", [])
    assert isinstance(refs, list)
    for ref in refs:
        assert set(ref) == {"anchor_id", "graph_id", "anchor_type", "table_ref" if ref["anchor_type"] == "table" else "clause_ref"}


@pytest.mark.parametrize("metadata", METADATA_CASES)
def test_postgresql_jsonb_codec_preserves_metadata_shape(metadata):
    column = DocumentChunk.__table__.c.source_metadata
    assert column.nullable is True and isinstance(column.type, JSONB)
    codec = dialect()
    encoded = column.type.bind_processor(codec)(deepcopy(metadata))
    decoded = column.type.result_processor(codec, None)(encoded)
    assert_metadata(decoded, metadata)


@pytest.mark.parametrize("metadata", METADATA_CASES)
def test_index_payload_is_lossless_deterministic_and_does_not_mutate(
    db, document, settings, metadata,
):
    chunk = persist(db, document, metadata)
    original = deepcopy(chunk.source_metadata)
    first = search_index.build_chunk_index_payload(document, chunk, settings)
    second = search_index.build_chunk_index_payload(document, chunk, settings)
    assert chunk.source_metadata == original
    assert_metadata(first["source_metadata"], original or {})
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert "kg_refs" not in first and "kg_ref" not in first


@pytest.mark.parametrize("metadata", METADATA_CASES)
@pytest.mark.parametrize("channel", ["both", "keyword", "vector"])
def test_writer_embedding_index_hybrid_and_rag_preserve_metadata(
    db, document, settings, monkeypatch, metadata, channel,
):
    original = deepcopy(metadata)
    chunk = persist(db, document, metadata)
    assert_metadata(chunk.source_metadata, original)
    assert chunk.parse_run_id is None and chunk.block_mappings == []
    client = WireSearchClient(settings, channel)
    provider = encode_and_index(db, document, settings, monkeypatch, client)
    assert provider.document_calls == [[CONTENT]]
    assert_metadata(chunk.source_metadata, original)
    expected = original or {}  # Existing index contract normalizes null to {}.
    assert_metadata(client.sources[str(chunk.id)]["source_metadata"], expected)

    first_sources = deepcopy(client.sources)
    reindexed = search_index.rebuild_search_index(
        db, scope="document", document_id=document.id, settings=settings, client=client,
    )
    assert reindexed.indexed == reindexed.deleted == 1
    assert client.sources == first_sources
    assert_metadata(chunk.source_metadata, original)

    result = retrieve(db, settings, client, provider)
    assert result.total == 1
    assert result.items[0].retrieval_source == channel
    assert_metadata(result.items[0].source_metadata, expected)
    assert len(client.search_calls) == 2
    assert all("source_metadata" in body["_source"] for body in client.search_calls)
    assert_metadata(SearchData.model_validate(result).model_dump(mode="json")["items"][0]["source_metadata"], expected)

    # Exercise actual RAG orchestration/context/prompt/citations with a fake LLM.
    monkeypatch.setattr(rag, "retrieve_chunks", lambda *args, **kwargs: result)
    observed_contexts = []
    original_build = rag.build_context

    def capture_context(*args, **kwargs):
        context = original_build(*args, **kwargs)
        observed_contexts.append(context)
        return context

    monkeypatch.setattr(rag, "build_context", capture_context)
    llm = FakeLLMProvider(text="温度应 ≥720℃。[1]")
    answer = rag.answer_question(db, "Si 温度 壁厚", settings=settings, llm_provider=llm)
    assert answer.context_status == "ok" and len(llm.calls) == 1
    assert_metadata(observed_contexts[0].chunks[0].source_metadata, expected)
    assert observed_contexts[0].chunks[0].content == CONTENT
    public = RagAskData.from_service_result(answer).model_dump(mode="json")
    assert_metadata(public["retrieval"]["items"][0]["source_metadata"], expected)
    assert "source_metadata" not in public["citations"][0]
    assert "kg_refs" not in public and "kg_refs" not in public["citations"][0]
    prompt = "\n".join(part.text for message in llm.calls[0].messages for part in message.content)
    assert CONTENT in prompt
    for marker in (*CONTROL_TEXT, TABLE_REF["anchor_id"], CLAUSE_REF["anchor_id"],
                   TABLE_REF["graph_id"], TABLE_REF["table_ref"], "other_provenance"):
        assert marker not in prompt
        assert all(marker not in text for batch in provider.document_calls for text in batch)
    assert metadata == original


@pytest.mark.parametrize("absent", [True, False], ids=["absent", "explicit-null"])
@pytest.mark.parametrize("channel", ["both", "keyword", "vector"])
def test_legacy_opensearch_source_without_metadata_remains_searchable(
    db, document, settings, absent, channel,
):
    chunk = persist(db, document, None)
    source = search_index.build_chunk_index_payload(document, chunk, settings)
    if absent:
        source.pop("source_metadata")
    else:
        source["source_metadata"] = None
    client = WireSearchClient(settings, channel)
    client.sources[str(chunk.id)] = source
    result = retrieve(db, settings, client, FakeProvider())
    assert result.total == 1 and result.items[0].source_metadata is None
    assert SearchData.model_validate(result).items[0].source_metadata is None
    context = context_builder.build_rag_context("问题", result, settings)
    assert context.context_status == "ok"
    assert context.chunks[0].source_metadata is None
    assert (context.chunks[0].source_metadata or {}).get("kg_refs", []) == []


def test_source_metadata_mapping_remains_opaque_and_source_is_not_filtered(settings):
    body = build_casting_chunks_index_body(settings)
    mapping = body["mappings"]
    assert mapping["dynamic"] is False
    assert mapping["properties"]["source_metadata"] == {"type": "object", "enabled": False}
    assert "_source" not in mapping
    assert "dynamic_templates" not in mapping
    assert not {"kg_refs", "kg_ref", "anchor_id", "graph_id", "table_ref"}.intersection(mapping["properties"])
    assert body["aliases"] == {settings.search_index_alias: {}}


def test_public_search_adds_version_identity_preserving_rag_and_citation_fields():
    expected = {
        SearchRequest: {"query", "limit", "document_id"},
        SearchData: {"query", "limit", "total", "items"},
        SearchItem: {"chunk_id", "document_id", "original_filename", "chunk_index", "content",
                     "source_metadata", "retrieval_source", "keyword_score", "vector_score",
                     "keyword_rank", "vector_rank", "hybrid_score", "matched_keywords",
                     "embedding_model", "embedding_dim", "source_version", "graph_build_id", "chunk_set_id",
                     "source_start", "source_end", "content_sha256", "embedding_fingerprint"},
        RagAskRequest: {"question", "limit", "document_id"},
        RagAskData: {"question", "answer", "context_status", "citations", "retrieval", "llm", "graph"},
        RagCitationItem: {"citation_id", "chunk_id", "document_id", "original_filename",
                          "chunk_index", "content", "hybrid_score", "retrieval_source"},
    }
    for model, fields in expected.items():
        assert set(model.model_json_schema()["properties"]) == fields
    assert SearchItem.model_fields["source_metadata"].is_required() is False


@pytest.mark.parametrize("repeat", [1, 3])
def test_pdf_fixture_refs_survive_shared_writer_embedding_and_wire(db, document, settings, monkeypatch, repeat):
    drafts = [pdf_draft(CONTENT * repeat, [CLAUSE_REF, TABLE_REF])]
    expected = deepcopy(drafts[0].source_metadata)
    DocumentOperationGuard(db).lock_normal(document.id)
    chunks = DocumentChunkWriter(db).write(document_id=document.id, drafts=drafts)
    document.process_status = "parsed"
    db.commit()
    client = WireSearchClient(settings)
    provider = encode_and_index(db, document, settings, monkeypatch, client)
    assert [value for batch in provider.document_calls for value in batch] == [drafts[0].content]
    result = retrieve(db, settings, client, provider)
    context = context_builder.build_rag_context("温度", result, settings)
    assert len(context.chunks) == len(chunks) == 1
    assert_metadata(context.chunks[0].source_metadata, expected)
    assert all(marker not in context.chunks[0].content for marker in CONTROL_TEXT)


def test_rag_text_truncation_keeps_internal_metadata(db, document, settings):
    chunk = persist(db, document, provenance([TABLE_REF, CLAUSE_REF]))
    client = WireSearchClient(settings)
    source = search_index.build_chunk_index_payload(document, chunk, settings)
    source["content"] = CONTENT * 100
    client.sources[str(chunk.id)] = source
    result = retrieve(db, settings, client, FakeProvider())
    settings.rag_context_max_chars = 400
    context = context_builder.build_rag_context("温度", result, settings)
    assert context.chunks[0].was_truncated is True
    assert_metadata(context.chunks[0].source_metadata, chunk.source_metadata)
