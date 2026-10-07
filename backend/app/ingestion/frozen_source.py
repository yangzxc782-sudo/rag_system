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

from app.ingestion.pdf_cleaner import render_cleaned_block

RENDERER_VERSION = "pdf-canonical-v1"
CLEANER_VERSION = "pdf-cleaner-v1"
COORDINATE_UNIT = "unicode_code_point"
NORMALIZATION = "LF_NFC_UTF8_v1"
_CONTROL_START = re.compile(r"<!--\s*kg-anchor-(?:start|end)\b", re.I)


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


def render_frozen_source(
    blocks: Sequence[Any], *, document_id: UUID, parse_run_id: UUID,
    source_version: UUID, output_prefix: str,
) -> FrozenSource:
    entries, parts = [], []
    offset, previous_index = 0, -1
    identities: set[UUID] = set()
    for block in blocks:
        if (getattr(block, "document_id", None) != document_id
                or getattr(block, "parse_run_id", None) != parse_run_id):
            raise ValueError("Frozen source block ownership mismatch")
        if not isinstance(block.id, UUID) or block.id in identities or block.block_index <= previous_index:
            raise ValueError("Frozen source requires unique block IDs in stable index order")
        identities.add(block.id)
        previous_index = block.block_index
        rendered, _ = render_cleaned_block(block, output_prefix=output_prefix)
        rendered = unicodedata.normalize("NFC", strip_kg_markers(rendered).replace("\r\n", "\n").replace("\r", "\n"))
        if not parts:
            rendered = rendered.removeprefix("\ufeff")
        if "\x00" in rendered:
            raise ValueError("Canonical text cannot contain NUL")
        if not rendered.strip():
            continue
        if parts:
            parts.append("\n\n")
            offset += 2
        start = offset
        parts.append(rendered)
        offset += len(rendered)
        entries.append({
            "block_id": str(block.id), "block_index": block.block_index,
            "block_key": block.block_key, "block_type": block.block_type,
            "page_start": block.page_start, "page_end": block.page_end,
            "source_start": start, "source_end": offset,
        })
    if not parts:
        raise ValueError("PDF cleaning produced no canonical content")
    canonical = "".join(parts).encode("utf-8")
    directory = json_bytes({
        "schema_version": 1, "document_id": str(document_id), "parse_run_id": str(parse_run_id),
        "source_version": str(source_version), "coordinate_unit": COORDINATE_UNIT,
        "normalization": NORMALIZATION, "renderer_version": RENDERER_VERSION,
        "canonical_sha256": sha256_bytes(canonical), "character_count": offset,
        "separator": "\n\n", "blocks": entries,
    })
    return FrozenSource(source_version, canonical, directory, offset, sha256_bytes(canonical), sha256_bytes(directory))


def verify_frozen_source(
    canonical: bytes, block_map: bytes, *, source_version: UUID, document_id: UUID,
    parse_run_id: UUID, canonical_sha256: str, block_map_sha256: str, character_count: int,
) -> str:
    """Validate persisted bytes without normalizing, finding text or repairing offsets."""
    if sha256_bytes(canonical) != canonical_sha256 or sha256_bytes(block_map) != block_map_sha256:
        raise ValueError("Frozen source hash mismatch")
    text = canonical.decode("utf-8")
    if (len(text) != character_count or "\r" in text or "\x00" in text
            or text.startswith("\ufeff") or unicodedata.normalize("NFC", text) != text
            or _CONTROL_START.search(text)):
        raise ValueError("Frozen source text contract mismatch")
    directory = json.loads(block_map)
    expected = dict(schema_version=1, document_id=str(document_id), parse_run_id=str(parse_run_id),
                    source_version=str(source_version), coordinate_unit=COORDINATE_UNIT,
                    normalization=NORMALIZATION, renderer_version=RENDERER_VERSION,
                    canonical_sha256=canonical_sha256, character_count=character_count, separator="\n\n")
    if any(directory.get(key) != value for key, value in expected.items()):
        raise ValueError("Frozen source directory identity mismatch")
    last_end, last_index, seen = 0, -1, set()
    blocks = directory.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("Frozen source directory is empty")
    for entry in blocks:
        start, end, index = entry["source_start"], entry["source_end"], entry["block_index"]
        identity = UUID(entry["block_id"])
        if (any(type(value) is not int for value in (start, end, index))
                or not 0 <= start < end <= len(text) or index <= last_index or identity in seen
                or start != (last_end + 2 if seen else 0)
                or (seen and text[last_end:start] != "\n\n")):
            raise ValueError("Frozen source directory range mismatch")
        last_end, last_index = end, index
        seen.add(identity)
    if last_end != len(text):
        raise ValueError("Frozen source directory does not cover canonical text")
    return text
