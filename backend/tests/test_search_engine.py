from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from app.core.errors import SEARCH_ENGINE_CONFIG_INVALID, BusinessError
from app.search_engine import client as search_client
from app.search_engine.index_schema import (
    build_casting_chunks_index_body,
    build_casting_chunks_index_mapping,
    get_index_alias,
    get_index_name,
)


def make_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "search_engine_provider": "opensearch",
        "search_engine_url": "http://localhost:9200",
        "search_engine_username": "",
        "search_engine_password": "",
        "search_engine_verify_ssl": False,
        "search_engine_timeout_seconds": 30,
        "search_engine_max_retries": 3,
        "search_index_name": "casting_chunks_v1",
        "search_index_alias": "casting_chunks_current",
        "search_vector_space": "cosine",
        "search_content_analyzer": "ik_max_word",
        "search_query_analyzer": "ik_smart",
        "embedding_dim": 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def install_fake_opensearch(monkeypatch: pytest.MonkeyPatch) -> type:
    class FakeOpenSearch:
        instances: list["FakeOpenSearch"] = []

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            FakeOpenSearch.instances.append(self)

    fake_module = types.ModuleType("opensearchpy")
    fake_module.OpenSearch = FakeOpenSearch
    monkeypatch.setitem(sys.modules, "opensearchpy", fake_module)
    return FakeOpenSearch


def test_client_module_import_does_not_create_opensearch_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "opensearchpy", raising=False)

    importlib.reload(search_client)

    assert "opensearchpy" not in sys.modules


def test_create_opensearch_client_passes_local_http_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = install_fake_opensearch(monkeypatch)

    client = search_client.create_search_engine_client(make_settings())

    assert client is fake_client.instances[0]
    kwargs = fake_client.instances[0].kwargs
    assert kwargs["hosts"] == [{"host": "localhost", "port": 9200}]
    assert kwargs["http_auth"] is None
    assert kwargs["use_ssl"] is False
    assert kwargs["verify_certs"] is False
    assert kwargs["timeout"] == 30
    assert kwargs["max_retries"] == 3
    assert kwargs["retry_on_timeout"] is True


def test_create_opensearch_client_supports_http_auth_and_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = install_fake_opensearch(monkeypatch)
    settings = make_settings(
        search_engine_url="https://search.local:9443",
        search_engine_username="user",
        search_engine_password="pass",
        search_engine_verify_ssl=True,
        search_engine_timeout_seconds=15,
        search_engine_max_retries=2,
    )

    search_client.create_search_engine_client(settings)

    kwargs = fake_client.instances[0].kwargs
    assert kwargs["hosts"] == [{"host": "search.local", "port": 9443}]
    assert kwargs["http_auth"] == ("user", "pass")
    assert kwargs["use_ssl"] is True
    assert kwargs["verify_certs"] is True
    assert kwargs["timeout"] == 15
    assert kwargs["max_retries"] == 2


def test_unsupported_provider_raises_config_error() -> None:
    with pytest.raises(BusinessError) as exc_info:
        search_client.validate_search_engine_settings(make_settings(search_engine_provider="elasticsearch"))

    assert exc_info.value.code == SEARCH_ENGINE_CONFIG_INVALID


def test_missing_search_engine_url_raises_config_error() -> None:
    with pytest.raises(BusinessError) as exc_info:
        search_client.validate_search_engine_settings(make_settings(search_engine_url=""))

    assert exc_info.value.code == SEARCH_ENGINE_CONFIG_INVALID


def test_partial_auth_raises_config_error() -> None:
    with pytest.raises(BusinessError) as exc_info:
        search_client.validate_search_engine_settings(
            make_settings(search_engine_username="user", search_engine_password="")
        )

    assert exc_info.value.code == SEARCH_ENGINE_CONFIG_INVALID


def test_index_name_and_alias_come_from_settings() -> None:
    settings = make_settings(search_index_name="chunks_v9", search_index_alias="chunks_current")

    assert get_index_name(settings) == "chunks_v9"
    assert get_index_alias(settings) == "chunks_current"


def test_index_body_contains_settings_mappings_and_alias() -> None:
    body = build_casting_chunks_index_body(make_settings())

    assert body["settings"]["index"]["knn"] is True
    assert "mappings" in body
    assert body["aliases"] == {"casting_chunks_current": {}}


def test_mapping_contains_ik_content_fields() -> None:
    mapping = build_casting_chunks_index_mapping(make_settings())
    properties = mapping["properties"]

    assert properties["content"]["analyzer"] == "ik_max_word"
    assert properties["content"]["search_analyzer"] == "ik_smart"
    assert properties["content"]["fields"]["max"]["analyzer"] == "ik_max_word"
    assert properties["content"]["fields"]["smart"]["analyzer"] == "ik_smart"
    assert properties["content_max"]["analyzer"] == "ik_max_word"
    assert properties["content_smart"]["analyzer"] == "ik_smart"


def test_mapping_contains_exact_terms_keyword_and_metadata_disabled_object() -> None:
    mapping = build_casting_chunks_index_mapping(make_settings())
    properties = mapping["properties"]

    assert properties["exact_terms"]["type"] == "keyword"
    assert properties["source_metadata"] == {"type": "object", "enabled": False}
    assert mapping["dynamic"] is False


def test_mapping_contains_1024_dimensional_cosine_vector() -> None:
    mapping = build_casting_chunks_index_mapping(make_settings())
    embedding = mapping["properties"]["embedding"]

    assert embedding["type"] == "knn_vector"
    assert embedding["dimension"] == 1024
    assert embedding["space_type"] == "cosinesimil"


def test_mapping_contains_expected_chunk_document_and_status_fields() -> None:
    mapping = build_casting_chunks_index_mapping(make_settings())
    properties = mapping["properties"]

    assert properties["chunk_id"]["type"] == "keyword"
    assert properties["document_id"]["type"] == "keyword"
    assert properties["original_filename"]["fields"]["raw"]["type"] == "keyword"
    assert properties["chunk_index"]["type"] == "integer"
    assert properties["chunk_type"]["type"] == "keyword"
    assert properties["page_start"]["type"] == "integer"
    assert properties["page_end"]["type"] == "integer"
    assert properties["section_title"]["fields"]["raw"]["type"] == "keyword"
    assert properties["embedding_model"]["type"] == "keyword"
    assert properties["embedding_dim"]["type"] == "integer"
    assert properties["embedding_status"]["type"] == "keyword"
    assert properties["document_process_status"]["type"] == "keyword"
    assert properties["created_at"]["type"] == "date"
    assert properties["updated_at"]["type"] == "date"
