"""Create-only ChunkSet indices. No aliases, replacement, or deletion operations."""
import json
import logging
from uuid import UUID

from app.core.errors import BusinessError
from app.ingestion.frozen_source import json_bytes, sha256_bytes
from app.search_engine.client import create_search_engine_client
from app.search_engine.index_schema import build_casting_chunks_index_mapping, build_casting_chunks_index_settings
from app.services.embedding_contract import vector32


def index_name(set_id: UUID) -> str:
    return f"pdf-kg-chunks-v2-{set_id.hex}"


def payload_digest(rows: list[dict]) -> str:
    return sha256_bytes(json_bytes(sorted(rows, key=lambda row: row["chunk_id"])))


logger = logging.getLogger(__name__)
_MISSING = object()
_OWNER_KEYS = frozenset({
    "document_id", "source_version", "graph_build_id", "chunk_set_id", "schema_version",
    "manifest_sha256", "embedding_fingerprint", "payload_sha256", "chunk_count",
})
_CAUSE_CODES = frozenset({
    "SEARCH_ENGINE_CONFIG_INVALID", "CHUNK_PUBLICATION_CONFLICT", "CHUNK_LEASE_LOST",
    "CHUNK_GRAPH_NOT_READY", "DOCUMENT_NOT_FOUND", "DOCUMENT_DELETION_IN_PROGRESS",
    "DOCUMENT_DELETION_STATE_INCONSISTENT", "DOCUMENT_DELETE_FAILED",
})


def _value_type(value):
    if value is _MISSING:
        return "missing"
    return type(value).__name__ if type(value) in (dict, list, str, bool, int, float, type(None)) else "other"


def _mismatch(path, kind, actual, expected):
    # Expected schema keys define paths; never use keys from the upstream response.
    # Extra caller-owned metadata keys are not safe diagnostic identifiers.
    if path and path[0] == "_meta":
        path = tuple(key if key == "_meta" or key in _OWNER_KEYS else "<key>" for key in path)
    return dict(mismatch_path=".".join(path) or "<root>", mismatch_kind=kind,
                expected_type=_value_type(expected), actual_type=_value_type(actual))


def _mapping_mismatch(actual, expected, *, _path=(), _context="mapping"):
    """Return the first mismatch; normalize only known mapping representations.

    _meta and other arbitrary objects never acquire mapping/field semantics.
    Neither the create payload nor the server response is mutated.
    """
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return _mismatch(_path, "type_mismatch", actual, expected)
        for key, value in expected.items():
            path = (*_path, key)
            if key not in actual:
                if (_context == "field" and key == "search_analyzer"
                        and expected.get("type") == actual.get("type") == "text"
                        and isinstance(value, str) and value
                        and expected.get("analyzer") == actual.get("analyzer") == value):
                    continue
                return _mismatch(path, "missing_key", _MISSING, value)
            received = actual[key]
            if (_context in {"mapping", "field"} and key == "dynamic"
                    and ((type(value) is bool and type(received) is str
                          and received == ("true" if value else "false"))
                         or (type(received) is bool and type(value) is str
                             and value == ("true" if received else "false")))):
                received = value
            context = "other"
            if (_context in {"mapping", "field"} and key == "properties"
                    or _context == "field" and key == "fields"):
                context = "field_map"
            elif _context == "field_map":
                context = "field"
            mismatch = _mapping_mismatch(received, value, _path=path, _context=context)
            if mismatch:
                return mismatch
        return None
    if type(actual) is not type(expected):
        return _mismatch(_path, "type_mismatch", actual, expected)
    if actual != expected:
        return _mismatch(_path, "scalar_mismatch", actual, expected)
    return None


def _error_attribute(exc, name):
    try:
        return getattr(exc, name, None)
    except Exception:
        return None


def indexing_failure_details(exc):
    """Log-only whitelist: never stringify exceptions, responses or request data."""
    status = _error_attribute(exc, "status_code")
    code = _error_attribute(exc, "code") if isinstance(exc, BusinessError) else None
    cause_type = type(exc).__name__
    return dict(
        cause_type=cause_type if cause_type.isascii() and cause_type.isidentifier() and len(cause_type) <= 64 else "Exception",
        cause_code=(code if isinstance(code, str) and code in _CAUSE_CODES else "UNCLASSIFIED") if isinstance(exc, BusinessError) else None,
        http_status=status if type(status) is int and 100 <= status <= 599 else None,
    )


def _document_id(owner):
    try:
        return str(UUID(str(owner.get("document_id"))))
    except (AttributeError, TypeError, ValueError):
        return None


class VersionedIndex:
    def __init__(self, settings, client=None):
        self.settings = settings
        self._owns_client = client is None
        self.client = client or create_search_engine_client(settings, max_retries=0, retry_on_timeout=False)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def publish(self, set_id: UUID, owner: dict, rows: list[dict]) -> dict:
        operation, name, mismatch = "prepare_index", None, None
        try:
            if not rows or len(rows) > self.settings.pdf_kg_max_chunks or len({r["chunk_id"] for r in rows}) != len(rows):
                raise ValueError("Invalid versioned index size/identities")
            name = index_name(set_id)
            metadata = {**owner, "schema_version": 2, "chunk_set_id": str(set_id),
                        "payload_sha256": payload_digest(rows), "chunk_count": len(rows)}
            mapping = build_casting_chunks_index_mapping(self.settings)
            mapping["_meta"] = metadata
            for key in ("source_version", "graph_build_id", "chunk_set_id", "content_sha256", "embedding_fingerprint"):
                mapping["properties"][key] = {"type": "keyword"}
            for key in ("source_start", "source_end"):
                mapping["properties"][key] = {"type": "long"}
            mapping["properties"]["schema_version"] = {"type": "integer"}
            client = self.client
            operation = "exists"
            if not client.indices.exists(index=name):
                # An ambiguous create outcome is recovered by the next explicit retry.
                operation = "create"
                client.indices.create(index=name, body={"settings": build_casting_chunks_index_settings(self.settings),
                                                         "mappings": mapping})
            operation = "get_mapping"
            actual = client.indices.get_mapping(index=name)
            operation = "validate_mapping"
            if not isinstance(actual, dict) or set(actual) != {name}:
                mismatch = _mismatch((), "index_identity_mismatch", actual, {name: {}})
            else:
                mismatch = _mapping_mismatch(actual[name]["mappings"], mapping)
            if mismatch:
                raise ValueError("Index identity/mapping conflict")
            operation = "get_settings"
            config = client.indices.get_settings(index=name)[name]["settings"]["index"]
            blocked = str(config.get("blocks", {}).get("write", "false")).lower() == "true"
            if not blocked:
                for start in range(0, len(rows), 100):
                    batch = rows[start:start + 100]
                    body = []
                    for row in batch:
                        body.extend(({"create": {"_index": name, "_id": row["chunk_id"]}}, row))
                    operation = "bulk"
                    result = client.bulk(body=body, refresh="wait_for")
                    operation = "validate_bulk"
                    items = result.get("items", [])
                    if len(items) != len(batch) or any(item.get("create", {}).get("status") not in (201, 409) for item in items):
                        raise ValueError("Partial index creation; explicit retry required")
                operation = "seal"
                client.indices.put_settings(index=name, body={"index.blocks.write": True})
            # Verify the sealed physical index, including pre-existing 409 records.
            operation = "verify_seal"
            config = client.indices.get_settings(index=name)[name]["settings"]["index"]
            if str(config.get("blocks", {}).get("write", "false")).lower() != "true":
                raise ValueError("Index was not sealed")
            operation = "readback"
            response = client.search(index=name, body={"query": {"match_all": {}}, "size": len(rows) + 1,
                                                       "track_total_hits": True}, request_timeout=30)
            operation = "validate_readback"
            if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
                raise ValueError("Incomplete index verification")
            hits = response.get("hits", {})
            total = hits.get("total", {})
            if total != {"value": len(rows), "relation": "eq"} or len(hits.get("hits", [])) != len(rows):
                raise ValueError("Index count mismatch")
            received = []
            for hit in hits["hits"]:
                row = dict(hit["_source"])
                if hit.get("_index") != name or hit["_id"] != row["chunk_id"]:
                    raise ValueError("Index record identity mismatch")
                row["embedding"] = vector32(row["embedding"], self.settings.embedding_dim)
                received.append(row)
            operation = "verify_payload"
            if payload_digest(received) != metadata["payload_sha256"]:
                raise ValueError("Index payload mismatch")
            return {**metadata, "index_name": name, "verified": True, "write_blocked": True}
        except Exception as exc:
            diagnostics = dict(document_id=_document_id(owner), chunk_set_id=str(set_id),
                stage="indexing", operation=operation, index_name=name, **indexing_failure_details(exc))
            if mismatch:
                diagnostics.update(mismatch)
            logger.warning("Versioned index failed: %s", json.dumps(diagnostics, ensure_ascii=False),
                           extra={"event": "versioned_index_failed", **diagnostics})
            raise
