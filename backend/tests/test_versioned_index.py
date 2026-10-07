"""OpenSearch protocol doubles; real cluster/analyzer verification is separate."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.core.errors import BusinessError
from app.search_engine.index_schema import build_casting_chunks_index_mapping
from app.search_engine.versioned_index import VersionedIndex, _mapping_mismatch, index_name


def opensearch_36_mapping(mapping):
    """Observed GET representation, using only synthetic schema/ownership data."""
    mapping = deepcopy(mapping)
    mapping["dynamic"] = "false"
    mapping["properties"]["content"]["fields"]["smart"].pop("search_analyzer")
    mapping["properties"]["content_smart"].pop("search_analyzer")
    return mapping


class Client:
    def __init__(self, *, normalize=False):
        self.indices = self
        self.mapping, self.rows, self.blocked = None, {}, False
        self.calls = []
        self.partial = False
        self.normalize = normalize
        self.created_body = None
        self.bulk_statuses = []
        self.search_calls = 0
    def exists(self, **kwargs):
        return self.mapping is not None
    def create(self, *, index, body):
        assert "aliases" not in body
        self.calls.append("create")
        self.created_body = deepcopy(body)
        self.mapping = deepcopy(body["mappings"])
    def get_mapping(self, *, index):
        mapping = opensearch_36_mapping(self.mapping) if self.normalize else deepcopy(self.mapping)
        return {index: {"mappings": mapping}}
    def get_settings(self, *, index):
        return {index: {"settings": {"index": {"blocks": {"write": str(self.blocked).lower()}}}}}
    def put_settings(self, *, index, body):
        assert body == {"index.blocks.write": True}
        self.calls.append("seal")
        self.blocked = True
    def bulk(self, *, body, refresh):
        assert not self.blocked
        self.calls.append("bulk")
        items = []
        for action, row in zip(body[::2], body[1::2], strict=True):
            assert list(action) == ["create"]
            key = action["create"]["_id"]
            status = 409 if key in self.rows else 201
            if self.partial and self.rows:
                status = 503
            else:
                self.rows.setdefault(key, deepcopy(row))
            items.append({"create": {"status": status}})
            self.bulk_statuses.append(status)
        return {"items": items, "errors": any(item["create"]["status"] >= 400 for item in items)}
    def search(self, *, index, body, request_timeout):
        assert self.blocked
        self.search_calls += 1
        return {"hits": {"total": {"value": len(self.rows), "relation": "eq"},
            "hits": [{"_id": k, "_index": index, "_source": v} for k, v in self.rows.items()]}}


def inputs():
    settings = Settings(_env_file=None)
    set_id = uuid4()
    rows = [dict(chunk_id=str(uuid4()), embedding=[1.0] * 1024, content=f"正文{i}") for i in range(3)]
    owner = dict(source_version=str(uuid4()), document_id=str(uuid4()), graph_build_id=str(uuid4()),
                 manifest_sha256="a" * 64, embedding_fingerprint="b" * 64)
    return settings, set_id, rows, owner


def test_create_seal_full_verify_and_readonly_retry():
    settings, key, rows, owner = inputs()
    client = Client()
    writer = VersionedIndex(settings, client)
    receipt = writer.publish(key, owner, rows)
    assert receipt["index_name"] == index_name(key) and receipt["write_blocked"]
    assert client.calls == ["create", "bulk", "seal"]
    assert writer.publish(key, owner, rows) == receipt
    assert client.calls == ["create", "bulk", "seal"]
    with pytest.raises(ValueError, match="identity"):
        writer.publish(key, {**owner, "document_id": str(uuid4())}, rows)


def test_partial_retry_create_only_and_duplicate_conflicts_not_hidden():
    settings, key, rows, owner = inputs()
    client = Client(); client.partial = True
    writer = VersionedIndex(settings, client)
    with pytest.raises(ValueError, match="Partial"):
        writer.publish(key, owner, rows)
    assert not client.blocked and len(client.rows) == 1
    client.partial = False
    writer.publish(key, owner, rows)
    assert len(client.rows) == 3
    client.rows[rows[0]["chunk_id"]]["content"] = "foreign"
    with pytest.raises(ValueError, match="payload"):
        writer.publish(key, owner, rows)


def test_extra_records_and_mapping_conflicts_rejected():
    settings, key, rows, owner = inputs()
    client = Client(); writer = VersionedIndex(settings, client)
    writer.publish(key, owner, rows)
    client.rows[str(uuid4())] = rows[0]
    with pytest.raises(ValueError, match="count"):
        writer.publish(key, owner, rows)
    client.mapping["properties"]["source_start"] = {"type": "text"}
    with pytest.raises(ValueError, match="mapping"):
        writer.publish(key, owner, rows)


@pytest.fixture
def expected_mapping():
    mapping = build_casting_chunks_index_mapping(Settings(_env_file=None))
    mapping["_meta"] = dict(document_id=str(uuid4()), source_version=str(uuid4()), graph_build_id=str(uuid4()),
        chunk_set_id=str(uuid4()), schema_version=2, manifest_sha256="a" * 64,
        embedding_fingerprint="b" * 64, payload_sha256="c" * 64, chunk_count=3)
    return mapping


@pytest.mark.parametrize("representation", ["exact", "dynamic", "redundant", "opensearch36"])
def test_semantic_mapping_equivalence_without_mutation(expected_mapping, representation):
    actual = deepcopy(expected_mapping)
    if representation == "dynamic":
        actual["dynamic"] = "false"
    elif representation == "redundant":
        actual["properties"]["content_smart"].pop("search_analyzer")
    elif representation == "opensearch36":
        actual = opensearch_36_mapping(actual)
    before = deepcopy((actual, expected_mapping))
    assert _mapping_mismatch(actual, expected_mapping) is None
    assert (actual, expected_mapping) == before


@pytest.mark.parametrize("value", [False, True])
def test_dynamic_normalization_is_mapping_path_aware(value):
    expected = {"dynamic": value, "properties": {"nested": {"type": "object", "dynamic": value}}}
    actual = {"dynamic": str(value).lower(), "properties": {"nested": {"type": "object", "dynamic": str(value).lower()}}}
    assert _mapping_mismatch(actual, expected) is None
    assert _mapping_mismatch(expected, actual) is None
    # Arbitrary metadata with mapping-like keys must not acquire mapping semantics.
    assert _mapping_mismatch({"_meta": actual}, {"_meta": expected}) is not None
    assert _mapping_mismatch({"dynamic": int(value)}, {"dynamic": value}) is not None
    assert _mapping_mismatch({"dynamic": str(value).upper()}, {"dynamic": value}) is not None


def test_search_analyzer_omission_only_applies_to_text_field_nodes():
    field = {"type": "text", "analyzer": "ik_smart", "search_analyzer": "ik_smart"}
    omitted = {"type": "text", "analyzer": "ik_smart"}
    assert _mapping_mismatch({"_meta": omitted}, {"_meta": field}) is not None
    assert _mapping_mismatch(omitted, field) is not None  # Root is not a field node.
    for field_type in ("keyword", "object"):
        assert _mapping_mismatch({"properties": {"x": {**omitted, "type": field_type}}},
            {"properties": {"x": {**field, "type": field_type}}}) is not None


@pytest.mark.parametrize("path,value,remove", [
    ("dynamic", "true", False),
    ("properties.content.search_analyzer", None, True),
    ("properties.content.analyzer", "standard", False),
    ("properties.content_smart.search_analyzer", "standard", False),
    ("properties.content_smart.search_analyzer", None, False),
    ("properties.content_smart.analyzer", None, True),
    ("properties.embedding.dimension", 768, False),
    ("properties.embedding.dimension", "1024", False),
    ("properties.source_metadata.enabled", "false", False),
    ("properties.embedding.type", "dense_vector", False),
    ("properties.embedding.type", "object", False),
    ("properties.embedding.type", "text", False),
    ("_meta.document_id", "foreign", False),
    ("_meta.document_id", None, True),
    ("_meta.source_version", "foreign", False),
    ("_meta.graph_build_id", "foreign", False),
    ("_meta.chunk_set_id", "foreign", False),
    ("_meta.schema_version", 1, False),
    ("_meta.manifest_sha256", "foreign", False),
    ("_meta.embedding_fingerprint", "foreign", False),
    ("_meta.payload_sha256", "foreign", False),
    ("_meta.chunk_count", 4, False),
    ("_meta.chunk_count", "3", False),
])
def test_real_mapping_conflicts_remain_rejected(expected_mapping, path, value, remove):
    actual = deepcopy(expected_mapping)
    node = actual
    keys = path.split(".")
    for key in keys[:-1]:
        node = node[key]
    if remove:
        node.pop(keys[-1])
    else:
        node[keys[-1]] = value
    mismatch = _mapping_mismatch(actual, expected_mapping)
    assert mismatch["mismatch_path"] == path
    assert mismatch["mismatch_kind"] in {"missing_key", "scalar_mismatch", "type_mismatch"}
    assert "expected_type" in mismatch and "actual_type" in mismatch
    assert set(mismatch) == {"mismatch_path", "mismatch_kind", "expected_type", "actual_type"}


def test_full_opensearch36_shape_proceeds_to_bulk_without_changing_create_schema():
    settings, key, rows, owner = inputs()
    client = Client(normalize=True)
    receipt = VersionedIndex(settings, client).publish(key, owner, rows)
    assert receipt["verified"] and client.calls == ["create", "bulk", "seal"]
    assert client.search_calls == 1 and client.bulk_statuses == [201] * len(rows)
    sent = client.created_body["mappings"]
    returned = client.get_mapping(index=index_name(key))[index_name(key)]["mappings"]
    assert sent != returned and sent["dynamic"] is False and returned["dynamic"] == "false"
    assert sent["properties"]["content"]["fields"]["smart"]["search_analyzer"] == "ik_smart"
    assert sent["properties"]["content_smart"]["search_analyzer"] == "ik_smart"
    assert returned["_meta"] == sent["_meta"]


@pytest.mark.parametrize("conflict", ["mapping", "identity"])
def test_conflict_stops_before_bulk_and_logs_safe_first_mismatch(conflict, monkeypatch, caplog):
    settings, key, rows, owner = inputs()
    rows[0]["content"] = "SECRET_PDF_CONTENT"
    rows[0]["embedding"] = [0.123456789] * 1024
    client = Client(normalize=True)
    original = client.get_mapping
    def changed_mapping(*, index):
        result = original(index=index)
        if conflict == "mapping":
            result[index]["mappings"]["properties"]["embedding"]["type"] = "SECRET_RESPONSE Authorization sk-secret"
        else:
            result["SECRET_INDEX_NAME"] = result.pop(index)
        return result
    monkeypatch.setattr(client, "get_mapping", changed_mapping)
    with pytest.raises(ValueError, match="Index identity/mapping conflict"):
        VersionedIndex(settings, client).publish(key, owner, rows)
    assert client.calls == ["create"] and not client.bulk_statuses
    record, = [r for r in caplog.records if getattr(r, "event", None) == "versioned_index_failed"]
    assert record.document_id == owner["document_id"] and record.chunk_set_id == str(key)
    assert record.stage == "indexing" and record.index_name == index_name(key)
    assert record.operation == "validate_mapping" and record.cause_type == "ValueError"
    assert record.mismatch_path == ("properties.embedding.type" if conflict == "mapping" else "<root>")
    assert record.mismatch_kind == ("scalar_mismatch" if conflict == "mapping" else "index_identity_mismatch")
    assert record.http_status is None and record.cause_code is None and record.exc_info is None
    rendered = caplog.text + repr(record.__dict__)
    assert all(secret not in rendered for secret in ("SECRET_", "Authorization", "sk-secret", "0.123456789", repr(rows)))
    console = json.loads(record.getMessage().split("Versioned index failed: ", 1)[1])
    assert console["mismatch_path"] == record.mismatch_path and "expected_type" in console


@pytest.mark.parametrize("business", [False, True])
@pytest.mark.parametrize("method,operation", [
    ("create", "create"), ("get_mapping", "get_mapping"), ("get_settings", "get_settings"),
    ("bulk", "bulk"), ("put_settings", "seal"), ("search", "readback"),
])
def test_http_failure_diagnostics_do_not_log_exception_response_or_details(business, method, operation, monkeypatch, caplog):
    from opensearchpy.exceptions import TransportError
    settings, key, rows, owner = inputs()
    client = Client()
    failure = (BusinessError("SEARCH_ENGINE_CONFIG_INVALID", "SECRET_BODY", detail={"secret": "SECRET_KEY"}, status_code=503)
        if business else TransportError(403, "SECRET_BODY", {"Authorization": "SECRET_KEY"}))
    def denied(**kwargs):
        raise failure
    monkeypatch.setattr(client, method, denied)
    with pytest.raises(type(failure)) as caught:
        VersionedIndex(settings, client).publish(key, owner, rows)
    assert caught.value is failure
    record, = [r for r in caplog.records if getattr(r, "event", None) == "versioned_index_failed"]
    assert record.operation == operation and record.http_status == (503 if business else 403)
    assert record.cause_code == ("SEARCH_ENGINE_CONFIG_INVALID" if business else None)
    assert "SECRET" not in caplog.text + repr(record.__dict__) and record.exc_info is None


def test_failure_diagnostics_filter_untrusted_or_unavailable_metadata():
    from app.search_engine.versioned_index import indexing_failure_details
    unknown = BusinessError("SECRET_UNRECOGNIZED_CODE", "SECRET_BODY", status_code=500)
    assert indexing_failure_details(unknown)["cause_code"] == "UNCLASSIFIED"
    class UnavailableStatus(ValueError):
        @property
        def status_code(self):
            raise RuntimeError("SECRET_RESPONSE_BODY")
    assert indexing_failure_details(UnavailableStatus())["http_status"] is None
    for status in ("SECRET_STATUS", True, 0, 999):
        unknown.status_code = status
        assert indexing_failure_details(unknown)["http_status"] is None


def test_409_recovery_performs_full_validation_and_refuses_conflicting_content():
    settings, key, rows, owner = inputs()
    client = Client(normalize=True)
    client.partial = True
    writer = VersionedIndex(settings, client)
    with pytest.raises(ValueError, match="Partial"):
        writer.publish(key, owner, rows)
    assert len(client.rows) == 1 and client.search_calls == 0 and not client.blocked
    client.rows[rows[0]["chunk_id"]]["content"] = "conflicting historical content"
    client.partial = False
    with pytest.raises(ValueError, match="payload"):
        writer.publish(key, owner, rows)
    assert 409 in client.bulk_statuses and client.search_calls == 1
    assert client.calls == ["create", "bulk", "bulk", "seal"]
