from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import unquote
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.document import Document


DOCUMENT_DELETION_MANIFEST_INVALID = "DOCUMENT_DELETION_MANIFEST_INVALID"
DOCUMENT_DELETION_MANIFEST_VERSION_UNSUPPORTED = (
    "DOCUMENT_DELETION_MANIFEST_VERSION_UNSUPPORTED"
)
DOCUMENT_DELETION_MANIFEST_SCHEMA_VERSION = 1

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "document_id",
        "bucket_name",
        "raw_object_key",
        "derived_object_keys",
        "derived_prefixes",
        "parse_run_ids",
        "block_ids",
        "asset_ids",
        "chunk_ids",
        "knowledge_source_relation_ids",
        "knowledge_item_ids",
        "search_index_name",
        "search_index_alias",
    }
)
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_INDEX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class DocumentDeletionManifestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DocumentDeletionManifest:
    schema_version: int
    document_id: UUID
    bucket_name: str
    raw_object_key: str
    derived_object_keys: tuple[str, ...]
    derived_prefixes: tuple[str, ...]
    parse_run_ids: tuple[UUID, ...]
    block_ids: tuple[UUID, ...]
    asset_ids: tuple[UUID, ...]
    chunk_ids: tuple[UUID, ...]
    knowledge_source_relation_ids: tuple[UUID, ...]
    knowledge_item_ids: tuple[UUID, ...]
    search_index_name: str
    search_index_alias: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != DOCUMENT_DELETION_MANIFEST_SCHEMA_VERSION
        ):
            raise DocumentDeletionManifestError(
                DOCUMENT_DELETION_MANIFEST_VERSION_UNSUPPORTED,
                "Document deletion manifest version is unsupported.",
            )

        object.__setattr__(self, "bucket_name", _validated_bucket(self.bucket_name))
        object.__setattr__(
            self,
            "parse_run_ids",
            _normalized_uuids(self.parse_run_ids),
        )
        object.__setattr__(self, "block_ids", _normalized_uuids(self.block_ids))
        object.__setattr__(self, "asset_ids", _normalized_uuids(self.asset_ids))
        object.__setattr__(self, "chunk_ids", _normalized_uuids(self.chunk_ids))
        object.__setattr__(
            self,
            "knowledge_source_relation_ids",
            _normalized_uuids(self.knowledge_source_relation_ids),
        )
        object.__setattr__(
            self,
            "knowledge_item_ids",
            _normalized_uuids(self.knowledge_item_ids),
        )
        prefixes = _normalized_strings(self.derived_prefixes)
        for prefix in prefixes:
            _validate_derived_prefix(
                prefix,
                document_id=self.document_id,
                parse_run_ids=frozenset(self.parse_run_ids),
            )
        object.__setattr__(self, "derived_prefixes", prefixes)

        raw_object_key = _validate_raw_object_key(
            self.raw_object_key,
            document_id=self.document_id,
        )
        object.__setattr__(self, "raw_object_key", raw_object_key)

        derived_keys = _normalized_strings(self.derived_object_keys)
        for key in derived_keys:
            _validate_exact_object_key(key)
            if not any(key.startswith(prefix) for prefix in prefixes):
                _invalid("A derived object key is outside validated prefixes.")
        if raw_object_key in derived_keys or any(
            raw_object_key.startswith(prefix) for prefix in prefixes
        ):
            _invalid("Raw and derived object targets overlap.")
        object.__setattr__(self, "derived_object_keys", derived_keys)
        object.__setattr__(
            self,
            "search_index_name",
            _validated_search_target(self.search_index_name),
        )
        object.__setattr__(
            self,
            "search_index_alias",
            _validated_search_target(self.search_index_alias),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DocumentDeletionManifest":
        if not isinstance(payload, Mapping):
            _invalid("Document deletion manifest must be an object.")
        version = payload.get("schema_version")
        if (
            type(version) is not int
            or version != DOCUMENT_DELETION_MANIFEST_SCHEMA_VERSION
        ):
            raise DocumentDeletionManifestError(
                DOCUMENT_DELETION_MANIFEST_VERSION_UNSUPPORTED,
                "Document deletion manifest version is unsupported.",
            )
        if set(payload) != _MANIFEST_FIELDS:
            _invalid("Document deletion manifest fields are invalid.")

        return cls(
            schema_version=DOCUMENT_DELETION_MANIFEST_SCHEMA_VERSION,
            document_id=_uuid_value(payload["document_id"]),
            bucket_name=_string_value(payload["bucket_name"]),
            raw_object_key=_string_value(payload["raw_object_key"]),
            derived_object_keys=_string_sequence(payload["derived_object_keys"]),
            derived_prefixes=_string_sequence(payload["derived_prefixes"]),
            parse_run_ids=_uuid_sequence(payload["parse_run_ids"]),
            block_ids=_uuid_sequence(payload["block_ids"]),
            asset_ids=_uuid_sequence(payload["asset_ids"]),
            chunk_ids=_uuid_sequence(payload["chunk_ids"]),
            knowledge_source_relation_ids=_uuid_sequence(
                payload["knowledge_source_relation_ids"]
            ),
            knowledge_item_ids=_uuid_sequence(payload["knowledge_item_ids"]),
            search_index_name=_string_value(payload["search_index_name"]),
            search_index_alias=_string_value(payload["search_index_alias"]),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_id": str(self.document_id),
            "bucket_name": self.bucket_name,
            "raw_object_key": self.raw_object_key,
            "derived_object_keys": list(self.derived_object_keys),
            "derived_prefixes": list(self.derived_prefixes),
            "parse_run_ids": [str(value) for value in self.parse_run_ids],
            "block_ids": [str(value) for value in self.block_ids],
            "asset_ids": [str(value) for value in self.asset_ids],
            "chunk_ids": [str(value) for value in self.chunk_ids],
            "knowledge_source_relation_ids": [
                str(value) for value in self.knowledge_source_relation_ids
            ],
            "knowledge_item_ids": [str(value) for value in self.knowledge_item_ids],
            "search_index_name": self.search_index_name,
            "search_index_alias": self.search_index_alias,
        }


def build_document_deletion_manifest(
    db: Session,
    *,
    document: Document,
    settings: Any,
) -> DocumentDeletionManifest:
    """Snapshot deletion identifiers from a Document already locked by its caller."""

    del db
    document_id = _uuid_value(document.id)
    configured_root = _validated_namespace_root(settings.mineru_output_prefix)
    parse_runs = list(getattr(document, "parse_runs", ()) or ())
    assets = list(getattr(document, "assets", ()) or ())
    prefixes_by_run: dict[UUID, str] = {}
    derived_keys: list[str] = []

    for parse_run in parse_runs:
        parse_run_id = _uuid_value(parse_run.id)
        output_prefix = getattr(parse_run, "output_prefix", None)
        output_keys = [
            value
            for value in (
                getattr(parse_run, "output_markdown_key", None),
                getattr(parse_run, "output_json_key", None),
            )
            if value
        ]
        if output_prefix is None:
            _invalid("Persisted parse run has no recovery prefix.")
        normalized_prefix = _persisted_parse_run_prefix(
            _string_value(output_prefix),
            configured_root=configured_root,
            document_id=document_id,
            parse_run_id=parse_run_id,
        )
        prefixes_by_run[parse_run_id] = normalized_prefix
        derived_keys.extend(_string_value(value) for value in output_keys)

    for asset in assets:
        parse_run_id = _uuid_value(asset.parse_run_id)
        if parse_run_id not in prefixes_by_run:
            _invalid("Persisted asset has no validated parse-run prefix.")
        derived_keys.append(_string_value(asset.asset_key))

    sources = list(getattr(document, "knowledge_item_sources", ()) or ())
    return DocumentDeletionManifest(
        schema_version=DOCUMENT_DELETION_MANIFEST_SCHEMA_VERSION,
        document_id=document_id,
        bucket_name=_string_value(document.bucket_name),
        raw_object_key=_string_value(document.object_key),
        derived_object_keys=tuple(derived_keys),
        derived_prefixes=tuple(prefixes_by_run.values()),
        parse_run_ids=tuple(_uuid_value(run.id) for run in parse_runs),
        block_ids=tuple(
            _uuid_value(block.id)
            for block in (getattr(document, "blocks", ()) or ())
        ),
        asset_ids=tuple(_uuid_value(asset.id) for asset in assets),
        chunk_ids=tuple(
            _uuid_value(chunk.id)
            for chunk in (getattr(document, "chunks", ()) or ())
        ),
        knowledge_source_relation_ids=tuple(
            _uuid_value(source.id) for source in sources
        ),
        knowledge_item_ids=tuple(
            _uuid_value(source.knowledge_item_id) for source in sources
        ),
        search_index_name=_string_value(settings.search_index_name),
        search_index_alias=_string_value(settings.search_index_alias),
    )


def _persisted_parse_run_prefix(
    value: str,
    *,
    configured_root: str,
    document_id: UUID,
    parse_run_id: UUID,
) -> str:
    _validate_safe_path(value, allow_trailing_slash=True)
    normalized = value.rstrip("/")
    expected = f"{configured_root}/{document_id}/{parse_run_id}"
    if normalized != expected:
        _invalid("Persisted parse-run prefix is outside the configured namespace.")
    return f"{normalized}/"


def _validate_derived_prefix(
    value: str,
    *,
    document_id: UUID,
    parse_run_ids: frozenset[UUID],
) -> None:
    _validate_safe_path(value, require_trailing_slash=True)
    parts = value.rstrip("/").split("/")
    document_text = str(document_id)
    if parts.count(document_text) != 1:
        _invalid("Derived prefix is not scoped to the captured Document.")
    document_index = parts.index(document_text)
    if document_index < 1 or parts[0] == "raw":
        _invalid("Derived prefix root is invalid.")
    suffix = parts[document_index + 1 :]
    if len(suffix) > 1:
        _invalid("Derived prefix must end at the Document or parse-run segment.")
    if suffix:
        parse_run_id = _uuid_value(suffix[0])
        if parse_run_id not in parse_run_ids:
            _invalid("Derived prefix references an uncaptured parse run.")


def _validate_raw_object_key(value: str, *, document_id: UUID) -> str:
    _validate_exact_object_key(value)
    escaped_document = re.escape(str(document_id))
    pattern = re.compile(
        rf"^raw/[0-9]{{4}}/(0[1-9]|1[0-2])/{escaped_document}\.[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    if pattern.fullmatch(value) is None:
        _invalid("Raw object key does not match the captured Document identity.")
    return value


def _validate_exact_object_key(value: str) -> None:
    _validate_safe_path(value)
    if value.endswith("/"):
        _invalid("Exact object key must not be a prefix.")


def _validate_safe_path(
    value: str,
    *,
    allow_trailing_slash: bool = False,
    require_trailing_slash: bool = False,
) -> None:
    if not isinstance(value, str) or not value:
        _invalid("Object namespace is empty.")
    if require_trailing_slash and not value.endswith("/"):
        _invalid("Object prefix must end with a slash.")
    if not allow_trailing_slash and not require_trailing_slash and value.endswith("/"):
        _invalid("Object key must not end with a slash.")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        _invalid("Object namespace is not relative and normalized.")

    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if decoded != value and any(
        part in {"", ".", ".."} for part in decoded.replace("\\", "/").split("/")
    ):
        _invalid("Encoded path traversal is forbidden.")
    if "%" in decoded or "\\" in decoded or decoded.startswith("/"):
        _invalid("Encoded or absolute object namespace is forbidden.")

    normalized = value[:-1] if value.endswith("/") else value
    parts = normalized.split("/")
    if not parts or any(
        part in {"", ".", ".."} or _SAFE_PATH_SEGMENT.fullmatch(part) is None
        for part in parts
    ):
        _invalid("Object namespace contains an unsafe path segment.")


def _validated_namespace_root(value: Any) -> str:
    root = _string_value(value).strip("/")
    _validate_safe_path(root)
    if root == "raw":
        _invalid("Derived namespace root must not be raw.")
    return root


def _validated_bucket(value: str) -> str:
    if _BUCKET_PATTERN.fullmatch(value) is None or ".." in value:
        _invalid("Manifest bucket name is invalid.")
    return value


def _validated_search_target(value: str) -> str:
    if (
        _INDEX_PATTERN.fullmatch(value) is None
        or value in {"_all", "*"}
        or "*" in value
        or "," in value
    ):
        _invalid("Manifest search target is invalid.")
    return value


def _uuid_sequence(value: Any) -> tuple[UUID, ...]:
    if not isinstance(value, (list, tuple)):
        _invalid("Manifest identifier collection is invalid.")
    return tuple(_uuid_value(item) for item in value)


def _string_sequence(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _invalid("Manifest string collection is invalid.")
    return tuple(_string_value(item) for item in value)


def _uuid_value(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        _invalid("Manifest identifier is invalid.")
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        _invalid("Manifest identifier is invalid.")


def _string_value(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid("Manifest string value is invalid.")
    return value.strip()


def _normalized_uuids(values: Iterable[UUID]) -> tuple[UUID, ...]:
    return tuple(sorted(set(values), key=str))


def _normalized_strings(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _invalid(message: str) -> None:
    raise DocumentDeletionManifestError(DOCUMENT_DELETION_MANIFEST_INVALID, message)
