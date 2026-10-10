"""Render one immutable canonical text and its ordered block directory.

Coordinates are Python Unicode code points, [start, end). This module does not
extract KG units, split retrieval chunks, or project transformed extraction text.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
import unicodedata
from typing import Any, Sequence
from uuid import UUID

from app.ingestion.mineru.normalizer import SUPPORTED_BLOCK_TYPES
from app.ingestion.pdf_cleaner import render_cleaned_block

SOURCE_MAP_SCHEMA_VERSION = 2
RENDERER_VERSION = "pdf-canonical-v1"
CLEANER_VERSION = "pdf-cleaner-v1"
COORDINATE_UNIT = "unicode_code_point"
NORMALIZATION = "LF_NFC_UTF8_v1"
_CONTROL_START = re.compile(r"<!--\s*kg-anchor-(?:start|end)\b", re.I)
_BLOCK_FIELDS = frozenset({
    "block_id", "block_index", "block_key", "block_type", "page_start", "page_end",
    "source_start", "source_end", "section_path", "content_format", "asset_keys",
})
_CONTENT_FORMATS = frozenset({"plain_text", "markdown", "mixed"})


def sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def strip_kg_markers(text: str) -> str:
    """Remove whole reserved comments; incomplete controls block freezing."""
    parts, cursor = [], 0
    for match in _CONTROL_START.finditer(text):
        if match.start() < cursor:
            continue
        end = text.find("-->", match.end())
        if end < 0:
            raise ValueError("Unterminated KG control comment")
        parts.append(text[cursor:match.start()])
        cursor = end + 3
    parts.append(text[cursor:])
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class FrozenSource:
    source_version: UUID
    canonical: bytes
    block_map: bytes
    character_count: int
    canonical_sha256: str
    block_map_sha256: str


def _object_path(value: Any) -> str:
    if (type(value) is not str or not value or "\\" in value or ":" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("Invalid frozen source asset path")
    return value


def _asset_key(value: Any, output_prefix: str) -> str:
    key = _object_path(value)
    if not key.startswith(output_prefix + "/"):
        raise ValueError("Frozen source asset ownership mismatch")
    return key


def _string_list(value: Any) -> list[str]:
    if (type(value) is not list
            or any(type(item) is not str or not item.strip() or "\x00" in item for item in value)):
        raise ValueError("Invalid frozen source structural list")
    return list(value)


def _block_lists(block: Any) -> tuple[list[str], list[str]]:
    metadata = getattr(block, "source_metadata", None)
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("Invalid frozen source block metadata")
    result = []
    for name in ("section_path", "asset_keys"):
        direct = getattr(block, name, None)
        direct = [] if direct is None else _string_list(direct)
        inherited = _string_list(metadata[name]) if name in metadata else []
        result.append(direct or inherited)
    return result[0], list(dict.fromkeys(result[1]))


def _validate_structure(entry: dict, output_prefix: str) -> None:
    key, kind, format_ = entry["block_key"], entry["block_type"], entry["content_format"]
    if key is not None and (type(key) is not str or not key.strip() or "\x00" in key):
        raise ValueError("Invalid frozen source block key")
    if type(kind) is not str or kind not in SUPPORTED_BLOCK_TYPES or kind in {"header", "footer"}:
        raise ValueError("Invalid frozen source block type")
    if type(format_) is not str or format_ not in _CONTENT_FORMATS:
        raise ValueError("Invalid frozen source content format")
    start, end = entry["page_start"], entry["page_end"]
    if (any(page is not None and (type(page) is not int or page < 0) for page in (start, end))
            or start is not None and end is not None and end < start):
        raise ValueError("Invalid frozen source page range")
    _string_list(entry["section_path"])
    keys = _string_list(entry["asset_keys"])
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate frozen source asset key")
    for key in keys:
        _asset_key(key, output_prefix)


def render_frozen_source(
    blocks: Sequence[Any], *, document_id: UUID, parse_run_id: UUID,
    source_version: UUID, output_prefix: str, registered_asset_keys: Sequence[str],
) -> FrozenSource:
    output_prefix = _object_path(output_prefix)
    if not isinstance(registered_asset_keys, (list, tuple)):
        raise ValueError("Frozen source requires registered parse assets")
    registered = {_asset_key(key, output_prefix) for key in registered_asset_keys}
    entries, parts = [], []
    offset, previous_index = 0, -1
    identities: set[UUID] = set()
    active_section_path: list[str] = []
    for block in blocks:
        if (getattr(block, "document_id", None) != document_id
                or getattr(block, "parse_run_id", None) != parse_run_id):
            raise ValueError("Frozen source block ownership mismatch")
        if (not isinstance(block.id, UUID) or block.id in identities
                or type(block.block_index) is not int or block.block_index <= previous_index):
            raise ValueError("Frozen source requires unique block IDs in stable index order")
        identities.add(block.id)
        previous_index = block.block_index
        section_path, asset_keys = _block_lists(block)
        for key in asset_keys:
            if _asset_key(key, output_prefix) not in registered:
                raise ValueError("Frozen source references an unregistered parse asset")
        rendered, content_format = render_cleaned_block(block, output_prefix=output_prefix)
        rendered = unicodedata.normalize("NFC", strip_kg_markers(rendered).replace("\r\n", "\n").replace("\r", "\n"))
        if not parts:
            rendered = rendered.removeprefix("\ufeff")
        if "\x00" in rendered:
            raise ValueError("Canonical text cannot contain NUL")
        if not rendered.strip():
            continue
        if section_path:
            active_section_path = section_path
        elif block.block_type == "title":
            active_section_path = [rendered.strip()]
        if parts:
            parts.append("\n\n")
            offset += 2
        start = offset
        parts.append(rendered)
        offset += len(rendered)
        entry = {
            "block_id": str(block.id), "block_index": block.block_index,
            "block_key": block.block_key, "block_type": block.block_type,
            "page_start": block.page_start, "page_end": block.page_end,
            "source_start": start, "source_end": offset,
            "section_path": list(active_section_path), "content_format": content_format,
            "asset_keys": asset_keys,
        }
        _validate_structure(entry, output_prefix)
        entries.append(entry)
    if not parts:
        raise ValueError("PDF cleaning produced no canonical content")
    canonical = "".join(parts).encode("utf-8")
    directory = json_bytes({
        "schema_version": SOURCE_MAP_SCHEMA_VERSION, "document_id": str(document_id), "parse_run_id": str(parse_run_id),
        "source_version": str(source_version), "coordinate_unit": COORDINATE_UNIT,
        "normalization": NORMALIZATION, "renderer_version": RENDERER_VERSION,
        "canonical_sha256": sha256_bytes(canonical), "character_count": offset,
        "separator": "\n\n", "blocks": entries,
    })
    return FrozenSource(source_version, canonical, directory, offset, sha256_bytes(canonical), sha256_bytes(directory))


def verify_frozen_source(
    canonical: bytes, block_map: bytes, *, source_version: UUID, document_id: UUID,
    parse_run_id: UUID, canonical_sha256: str, block_map_sha256: str, character_count: int,
    output_prefix: str,
) -> str:
    """Validate persisted bytes without normalizing, finding text or repairing offsets."""
    output_prefix = _object_path(output_prefix)
    if sha256_bytes(canonical) != canonical_sha256 or sha256_bytes(block_map) != block_map_sha256:
        raise ValueError("Frozen source hash mismatch")
    text = canonical.decode("utf-8")
    if (type(character_count) is not int or character_count <= 0 or len(text) != character_count
            or "\r" in text or "\x00" in text
            or text.startswith("\ufeff") or unicodedata.normalize("NFC", text) != text
            or _CONTROL_START.search(text)):
        raise ValueError("Frozen source text contract mismatch")
    directory = json.loads(block_map)
    expected = dict(schema_version=SOURCE_MAP_SCHEMA_VERSION, document_id=str(document_id), parse_run_id=str(parse_run_id),
                    source_version=str(source_version), coordinate_unit=COORDINATE_UNIT,
                    normalization=NORMALIZATION, renderer_version=RENDERER_VERSION,
                    canonical_sha256=canonical_sha256, character_count=character_count, separator="\n\n")
    if (type(directory) is not dict or set(directory) != set(expected) | {"blocks"}
            or any(type(directory[key]) is not type(value) or directory[key] != value
                   for key, value in expected.items())):
        raise ValueError("Frozen source directory identity mismatch")
    last_end, last_index, seen = 0, -1, set()
    blocks = directory.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("Frozen source directory is empty")
    for entry in blocks:
        if type(entry) is not dict or set(entry) != _BLOCK_FIELDS:
            raise ValueError("Frozen source directory block fields mismatch")
        _validate_structure(entry, output_prefix)
        start, end, index = entry["source_start"], entry["source_end"], entry["block_index"]
        if type(entry["block_id"]) is not str:
            raise ValueError("Invalid frozen source block identity")
        identity = UUID(entry["block_id"])
        if (any(type(value) is not int for value in (start, end, index))
                or not 0 <= start < end <= len(text) or index <= last_index or identity in seen
                or str(identity) != entry["block_id"] or not text[start:end].strip()
                or start != (last_end + 2 if seen else 0)
                or (seen and text[last_end:start] != "\n\n")):
            raise ValueError("Frozen source directory range mismatch")
        last_end, last_index = end, index
        seen.add(identity)
    if last_end != len(text):
        raise ValueError("Frozen source directory does not cover canonical text")
    return text
