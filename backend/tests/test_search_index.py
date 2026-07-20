from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.api.v1 import search as search_api
from app.core.errors import (
    SEARCH_ENGINE_CONFIG_INVALID,
    SEARCH_ENGINE_UNAVAILABLE,
    SEARCH_INDEX_MAPPING_MISMATCH,
    SEARCH_INDEX_REBUILD_FAILED,
    BusinessError,
)
from app.schemas.search import SearchIndexRebuildRequest
from app.services import search_index
from app.services.search_index import (
    SearchIndexCreateResult,
    SearchIndexRebuildResult,
    SearchIndexStatusResult,
    SyncableChunkRow,
)
from app.search_engine.index_schema import build_casting_chunks_index_mapping


def make_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "search_engine_provider": "opensearch",
        "search_index_name": "casting_chunks_v1",
        "search_index_alias": "casting_chunks_current",
        "search_index_batch_size": 100,
        "search_vector_space": "cosine",
        "search_content_analyzer": "ik_max_word",
        "search_query_analyzer": "ik_smart",
        "embedding_dim": 1024,
        "embedding_model": "Qwen3-Embedding-0.6B",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeIndices:
    def __init__(self, *, exists: bool = True, alias_exists: bool = True, mapping: dict | None = None) -> None:
        self.exists_result = exists
        self.alias_exists_result = alias_exists
        self.mapping = mapping
        self.create_calls: list[dict[str, object]] = []
        self.put_alias_calls: list[dict[str, object]] = []
        self.calls: list[str] = []
        self.raise_on_exists: Exception | None = None

    def exists(self, *, index: str) -> bool:
        self.calls.append(f"exists:{index}")
        if self.raise_on_exists is not None:
            raise self.raise_on_exists
        return self.exists_result

    def create(self, *, index: str, body: dict) -> dict:
        self.calls.append(f"create:{index}")
        self.create_calls.append({"index": index, "body": body})
        return {"acknowledged": True}

    def get_mapping(self, *, index: str) -> dict:
        self.calls.append(f"get_mapping:{index}")
        if self.mapping is not None:
            return self.mapping
        return {
            index: {
                "mappings": build_casting_chunks_index_mapping(make_settings()),
            }
        }

    def exists_alias(self, *, name: str) -> bool:
        self.calls.append(f"exists_alias:{name}")
        return self.alias_exists_result

    def put_alias(self, *, index: str, name: str) -> dict:
        self.calls.append(f"put_alias:{index}:{name}")
        self.put_alias_calls.append({"index": index, "name": name})
        return {"acknowledged": True}


class FakeClient:
    def __init__(self, indices: FakeIndices | None = None) -> None:
        self.indices = indices or FakeIndices()
        self.bulk_calls: list[dict[str, object]] = []
        self.delete_by_query_calls: list[dict[str, object]] = []
        self.calls: list[str] = []
        self.bulk_response = {"errors": False, "items": []}
        self.delete_response = {"deleted": 0}
        self.count_response = {"count": 0}
        self.raise_on_bulk: Exception | None = None

    def bulk(self, **kwargs: object) -> dict:
        self.calls.append("bulk")
        self.bulk_calls.append(kwargs)
        if self.raise_on_bulk is not None:
            raise self.raise_on_bulk
        return self.bulk_response

    def delete_by_query(self, **kwargs: object) -> dict:
        self.calls.append("delete_by_query")
        self.delete_by_query_calls.append(kwargs)
        return self.delete_response

    def count(self, **kwargs: object) -> dict:
        self.calls.append("count")
        return self.count_response


class FakeDb:
    def __init__(self, document: object | None = None) -> None:
        self.document = document

    def get(self, model: object, document_id: UUID | None) -> object | None:
        return self.document


def make_document(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid4(),
        "original_filename": "casting.md",
        "process_status": "parsed",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_chunk(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid4(),
        "document_id": uuid4(),
        "chunk_index": 3,
        "content": "GB/T 标准下 HT250 冒口设计应保证热节补缩，避免缩孔。",
        "chunk_type": "text",
        "page_start": 1,
        "page_end": 2,
        "section_title": "冒口设计",
        "source_metadata": {"page_start": 1},
        "embedding": [0.1, 0.2, 0.3],
        "embedding_model": "Qwen3-Embedding-0.6B",
        "embedding_dim": 1024,
        "embedding_status": "embedded",
        "created_at": datetime(2026, 1, 1, 1, 2, 3),
        "updated_at": datetime(2026, 1, 2, 1, 2, 3),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_create_index_when_missing_calls_indices_create() -> None:
    client = FakeClient(FakeIndices(exists=False))

    result = search_index.create_or_update_search_index(FakeDb(), client=client, settings=make_settings())

    assert result.created is True
    assert result.alias_created is True
    assert client.indices.create_calls[0]["index"] == "casting_chunks_v1"
    assert "aliases" in client.indices.create_calls[0]["body"]


def test_create_index_existing_mapping_match_does_not_overwrite() -> None:
    client = FakeClient(FakeIndices(exists=True, alias_exists=True))

    result = search_index.create_or_update_search_index(FakeDb(), client=client, settings=make_settings())

    assert result.exists is True
    assert result.mapping_status == "matched"
    assert client.indices.create_calls == []


def test_create_index_existing_mapping_mismatch_raises() -> None:
    bad_mapping = {
        "casting_chunks_v1": {
            "mappings": {
                "properties": {
                    "content": {"type": "text"},
                    "exact_terms": {"type": "keyword"},
                    "source_metadata": {"type": "object", "enabled": False},
                    "embedding": {"type": "knn_vector", "dimension": 768, "space_type": "cosinesimil"},
                }
            }
        }
    }
    client = FakeClient(FakeIndices(exists=True, mapping=bad_mapping))

    with pytest.raises(BusinessError) as exc_info:
        search_index.create_or_update_search_index(FakeDb(), client=client, settings=make_settings())

    assert exc_info.value.code == SEARCH_INDEX_MAPPING_MISMATCH
    assert client.indices.create_calls == []


def test_create_index_existing_alias_missing_creates_alias() -> None:
    client = FakeClient(FakeIndices(exists=True, alias_exists=False))

    result = search_index.create_or_update_search_index(FakeDb(), client=client, settings=make_settings())

    assert result.alias_created is True
    assert client.indices.put_alias_calls == [{"index": "casting_chunks_v1", "name": "casting_chunks_current"}]


def test_rebuild_document_deletes_old_chunks_before_bulk(monkeypatch: pytest.MonkeyPatch) -> None:
    document_id = uuid4()
    document = make_document(id=document_id)
    chunk = make_chunk(document_id=document_id)
    client = FakeClient()
    client.delete_response = {"deleted": 4}
    client.bulk_response = {"errors": False, "items": [{"index": {"status": 201}}]}
    monkeypatch.setattr(search_index, "_load_syncable_chunks", lambda *args, **kwargs: [SyncableChunkRow(document, chunk)])

    result = search_index.rebuild_search_index(
        FakeDb(document=document),
        scope="document",
        document_id=document_id,
        client=client,
        settings=make_settings(),
    )

    assert client.calls == ["delete_by_query", "bulk"]
    assert client.delete_by_query_calls[0]["body"]["query"]["term"]["document_id"] == str(document_id)
    assert client.bulk_calls[0]["body"][0]["index"]["_id"] == str(chunk.id)
    assert result.deleted == 4
    assert result.indexed == 1


def test_rebuild_all_bulk_indexes_all_loaded_syncable_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    document = make_document()
    chunks = [make_chunk(chunk_index=0), make_chunk(chunk_index=1)]
    rows = [SyncableChunkRow(document, chunk) for chunk in chunks]
    client = FakeClient()
    client.bulk_response = {"errors": False, "items": [{"index": {"status": 201}}, {"index": {"status": 201}}]}
    monkeypatch.setattr(search_index, "_load_syncable_chunks", lambda *args, **kwargs: rows)

    result = search_index.rebuild_search_index(FakeDb(), scope="all", client=client, settings=make_settings())

    assert result.syncable_chunks == 2
    assert result.indexed == 2
    assert client.delete_by_query_calls == []
    assert len(client.bulk_calls[0]["body"]) == 4


def test_is_syncable_chunk_filters_embedding_status_dim_model_and_embedding() -> None:
    settings = make_settings()

    assert search_index.is_syncable_chunk(make_chunk(), settings) is True
    assert search_index.is_syncable_chunk(make_chunk(embedding_status="not_started"), settings) is False
    assert search_index.is_syncable_chunk(make_chunk(embedding_status="embed_failed"), settings) is False
    assert search_index.is_syncable_chunk(make_chunk(embedding=None), settings) is False
    assert search_index.is_syncable_chunk(make_chunk(embedding_dim=768), settings) is False
    assert search_index.is_syncable_chunk(make_chunk(embedding_model="other-model"), settings) is False


def test_is_syncable_chunk_uses_configured_model_and_dimension() -> None:
    settings = make_settings(embedding_model="configured-model", embedding_dim=768)

    assert search_index.is_syncable_chunk(
        make_chunk(embedding_model="configured-model", embedding_dim=768),
        settings,
    ) is True
    assert search_index.is_syncable_chunk(
        make_chunk(embedding_model="Qwen3-Embedding-0.6B", embedding_dim=1024),
        settings,
    ) is False


def test_build_payload_contains_source_metadata_embedding_and_exact_terms() -> None:
    document = make_document()
    chunk = make_chunk()

    payload = search_index.build_chunk_index_payload(document, chunk, make_settings())

    assert payload["chunk_id"] == str(chunk.id)
    assert payload["source_metadata"] == {"page_start": 1}
    assert payload["embedding"] == [0.1, 0.2, 0.3]
    assert payload["exact_terms"] == ["GB/T", "HT250", "冒口", "热节", "补缩", "缩孔"]


def test_extract_exact_terms_deduplicates_with_stable_order() -> None:
    content = "冒口 冒口 冷铁 QT450 GB/T HT250 缩孔 缩松 GB/T"

    terms = search_index.extract_exact_terms(content)

    assert terms == ["GB/T", "HT250", "QT450", "冒口", "冷铁", "缩孔", "缩松"]


def test_rebuild_no_syncable_chunks_returns_zero_without_bulk(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    monkeypatch.setattr(search_index, "_load_syncable_chunks", lambda *args, **kwargs: [])

    result = search_index.rebuild_search_index(FakeDb(), scope="all", client=client, settings=make_settings())

    assert result.syncable_chunks == 0
    assert result.indexed == 0
    assert client.bulk_calls == []


def test_rebuild_bulk_exception_raises_rebuild_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    document = make_document()
    chunk = make_chunk()
    client = FakeClient()
    client.raise_on_bulk = RuntimeError("bulk failed")
    monkeypatch.setattr(search_index, "_load_syncable_chunks", lambda *args, **kwargs: [SyncableChunkRow(document, chunk)])

    with pytest.raises(BusinessError) as exc_info:
        search_index.rebuild_search_index(FakeDb(), scope="all", client=client, settings=make_settings())

    assert exc_info.value.code == SEARCH_INDEX_REBUILD_FAILED
    assert chunk.embedding_status == "embedded"
    assert chunk.embedding == [0.1, 0.2, 0.3]
    assert chunk.embedding_model == "Qwen3-Embedding-0.6B"
    assert chunk.embedding_dim == 1024


def test_rebuild_bulk_partial_failure_raises_with_failed_chunk_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = make_document()
    successful_chunk = make_chunk(document_id=document.id, chunk_index=0)
    failed_chunk = make_chunk(document_id=document.id, chunk_index=1)
    rows = [
        SyncableChunkRow(document, successful_chunk),
        SyncableChunkRow(document, failed_chunk),
    ]
    client = FakeClient()
    client.bulk_response = {
        "errors": True,
        "items": [
            {"index": {"_id": str(successful_chunk.id), "status": 201}},
            {
                "index": {
                    "_id": str(failed_chunk.id),
                    "status": 400,
                    "error": {"type": "mapper_parsing_exception", "reason": "bad vector"},
                }
            },
        ],
    }
    monkeypatch.setattr(search_index, "_load_syncable_chunks", lambda *args, **kwargs: rows)

    with pytest.raises(BusinessError) as exc_info:
        search_index.rebuild_search_index(
            FakeDb(),
            scope="all",
            client=client,
            settings=make_settings(),
        )

    assert exc_info.value.code == SEARCH_INDEX_REBUILD_FAILED
    assert exc_info.value.detail["indexed"] == 1
    assert exc_info.value.detail["failed"] == 1
    assert str(failed_chunk.id) in str(exc_info.value.detail["errors"])
    assert "bad vector" in str(exc_info.value.detail["errors"])
    assert successful_chunk.embedding_status == "embedded"
    assert failed_chunk.embedding_status == "embedded"
    assert successful_chunk.embedding == [0.1, 0.2, 0.3]
    assert failed_chunk.embedding == [0.1, 0.2, 0.3]


def test_document_sync_indexes_25_mineru_chunks_with_stable_ids_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = uuid4()
    document = make_document(id=document_id, original_filename="test1.pdf")
    chunks = [
        make_chunk(document_id=document_id, chunk_index=index)
        for index in range(25)
    ]
    rows = [SyncableChunkRow(document, chunk) for chunk in chunks]
    client = FakeClient()
    client.bulk_response = {
        "errors": False,
        "items": [
            {"index": {"_id": str(chunk.id), "status": 201}}
            for chunk in chunks
        ],
    }
    monkeypatch.setattr(search_index, "_load_syncable_chunks", lambda *args, **kwargs: rows)

    client.delete_response = {"deleted": 0}
    first = search_index.rebuild_search_index(
        FakeDb(document=document),
        scope="document",
        document_id=document_id,
        client=client,
        settings=make_settings(),
    )
    client.delete_response = {"deleted": 25}
    second = search_index.rebuild_search_index(
        FakeDb(document=document),
        scope="document",
        document_id=document_id,
        client=client,
        settings=make_settings(),
    )

    first_ids = [item["index"]["_id"] for item in client.bulk_calls[0]["body"][::2]]
    second_ids = [item["index"]["_id"] for item in client.bulk_calls[1]["body"][::2]]
    expected_ids = [str(chunk.id) for chunk in chunks]
    assert first.syncable_chunks == first.indexed == 25
    assert second.syncable_chunks == second.indexed == 25
    assert first.deleted == 0
    assert second.deleted == 25
    assert first_ids == expected_ids
    assert second_ids == expected_ids
    assert len(set(first_ids)) == 25
    assert len(set(second_ids)) == 25
    assert set(first_ids) == set(second_ids)


def test_78_unembedded_chunks_are_not_syncable() -> None:
    settings = make_settings()
    chunks = [
        make_chunk(
            chunk_index=index,
            embedding_status="not_started",
            embedding=None,
            embedding_model=None,
            embedding_dim=None,
        )
        for index in range(78)
    ]

    assert [chunk for chunk in chunks if search_index.is_syncable_chunk(chunk, settings)] == []


def test_basic_parser_chunk_uses_the_same_syncable_contract() -> None:
    chunk = make_chunk(source_metadata={"parser_name": "simple"})

    assert search_index.is_syncable_chunk(chunk, make_settings()) is True


def test_rebuild_fails_cleanly_when_opensearch_is_unavailable() -> None:
    indices = FakeIndices()
    indices.raise_on_exists = ConnectionError("OpenSearch unavailable")
    client = FakeClient(indices)

    with pytest.raises(BusinessError) as exc_info:
        search_index.rebuild_search_index(
            FakeDb(),
            scope="all",
            client=client,
            settings=make_settings(),
        )

    assert exc_info.value.code == SEARCH_ENGINE_UNAVAILABLE
    assert exc_info.value.detail == {"error_type": "ConnectionError"}


def test_api_rebuild_partial_failure_returns_index_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_partial_failure(*args: object, **kwargs: object) -> None:
        raise BusinessError(
            SEARCH_INDEX_REBUILD_FAILED,
            "OpenSearch bulk indexing partially failed.",
            detail={"indexed": 1, "failed": 1, "errors": ["chunk_id=test status=400"]},
            status_code=500,
        )

    monkeypatch.setattr(search_api.search_index_service, "rebuild_search_index", raise_partial_failure)

    response = search_api.rebuild_search_index_endpoint(
        SearchIndexRebuildRequest(scope="document", document_id=uuid4()),
        FakeDb(),
    )

    assert response.status_code == 500
    body = bytes(response.body).decode("utf-8")
    assert SEARCH_INDEX_REBUILD_FAILED in body
    assert '"failed":1' in body


def test_get_status_returns_count_and_postgres_syncable_count(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(FakeIndices(exists=True, alias_exists=True))
    client.count_response = {"count": 9}
    monkeypatch.setattr(search_index, "count_postgres_syncable_chunks", lambda *args, **kwargs: 2)

    result = search_index.get_search_index_status(FakeDb(), client=client, settings=make_settings())

    assert result.search_engine_available is True
    assert result.index_document_count == 9
    assert result.postgres_syncable_chunks == 2
    assert result.errors == []


def test_api_create_index_endpoint_uses_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_api.search_index_service,
        "create_or_update_search_index",
        lambda db: SearchIndexCreateResult(
            index_name="casting_chunks_v1",
            alias="casting_chunks_current",
            created=True,
            exists=False,
            alias_created=True,
            mapping_status="created",
            message="created",
        ),
    )

    response = search_api.create_search_index_endpoint(FakeDb())

    assert response.success is True
    assert response.data.index_name == "casting_chunks_v1"


def test_api_rebuild_index_endpoint_scope_all_uses_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_api.search_index_service,
        "rebuild_search_index",
        lambda db, scope, document_id: SearchIndexRebuildResult(
            scope=scope,
            document_id=document_id,
            index_name="casting_chunks_v1",
            alias="casting_chunks_current",
            syncable_chunks=2,
            indexed=2,
            deleted=0,
            failed=0,
            errors=[],
            batch_size=100,
        ),
    )

    response = search_api.rebuild_search_index_endpoint(SearchIndexRebuildRequest(scope="all"), FakeDb())

    assert response.success is True
    assert response.data.indexed == 2


def test_api_rebuild_index_endpoint_scope_document_uses_service(monkeypatch: pytest.MonkeyPatch) -> None:
    document_id = uuid4()
    monkeypatch.setattr(
        search_api.search_index_service,
        "rebuild_search_index",
        lambda db, scope, document_id: SearchIndexRebuildResult(
            scope=scope,
            document_id=document_id,
            index_name="casting_chunks_v1",
            alias="casting_chunks_current",
            syncable_chunks=1,
            indexed=1,
            deleted=3,
            failed=0,
            errors=[],
            batch_size=100,
        ),
    )

    response = search_api.rebuild_search_index_endpoint(
        SearchIndexRebuildRequest(scope="document", document_id=document_id),
        FakeDb(),
    )

    assert response.success is True
    assert response.data.document_id == document_id
    assert response.data.deleted == 3


def test_api_rebuild_scope_document_missing_document_id_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_missing_document_id(*args: object, **kwargs: object) -> None:
        raise BusinessError(
            SEARCH_ENGINE_CONFIG_INVALID,
            "document_id is required when scope is document.",
            status_code=400,
        )

    monkeypatch.setattr(search_api.search_index_service, "rebuild_search_index", raise_missing_document_id)

    response = search_api.rebuild_search_index_endpoint(SearchIndexRebuildRequest(scope="document"), FakeDb())

    assert response.status_code == 400


def test_api_status_endpoint_uses_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_api.search_index_service,
        "get_search_index_status",
        lambda db: SearchIndexStatusResult(
            search_engine_available=True,
            index_name="casting_chunks_v1",
            alias="casting_chunks_current",
            index_exists=True,
            alias_exists=True,
            index_document_count=2,
            postgres_syncable_chunks=2,
            provider="opensearch",
            errors=[],
        ),
    )

    response = search_api.search_index_status_endpoint(FakeDb())

    assert response.success is True
    assert response.data.index_document_count == 2
