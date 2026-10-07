"""Retrieval-only segmentation: every chunk is one exact canonical substring."""
from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_right
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.extraction.kg_protocol import ANCHOR_ADAPTER
from app.ingestion.frozen_source import json_bytes, sha256_bytes

SEGMENTATION_VERSION = "sequential-codepoints-v1"


class SegmentationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    chunk_size: int = Field(default=1200, ge=1, le=60000)
    overlap: int = Field(default=120, ge=0, le=59999)
    boundary: Literal["characters", "paragraph", "line"] = "paragraph"

    @model_validator(mode="after")
    def overlap_fits(self):
        if self.overlap >= self.chunk_size:
            raise ValueError("overlap must be smaller than chunk_size")
        return self

    @property
    def fingerprint(self) -> str:
        return sha256_bytes(json_bytes(self.model_dump()))


@dataclass(frozen=True)
class SequentialChunk:
    content: str
    source_start: int
    source_end: int
    content_sha256: str
    kg_refs: tuple[dict, ...]
    block_ids: tuple[str, ...]
    page_start: int | None
    page_end: int | None


def overlaps(start: int, end: int, other_start: int, other_end: int) -> bool:
    return max(start, other_start) < min(end, other_end)


def split_sequential(text: str, config: SegmentationConfig, *, document_id: UUID, source_version: UUID,
                     graph_build_id: UUID, anchors: tuple[dict, ...], blocks: list[dict],
                     max_chunks: int = 5000) -> tuple[SequentialChunk, ...]:
    if not text or re.search(r"<!--\s*kg-anchor-(?:start|end)\b", text, re.I):
        raise ValueError("Canonical source is empty or contains KG controls")
    refs = {}
    for entry in anchors:
        if any(entry[key] != value for key, value in (("document_id", document_id),
                ("source_version", source_version), ("graph_build_id", graph_build_id))):
            raise ValueError("Anchor belongs to another source/build")
        start, end = entry["source_start"], entry["source_end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError("Invalid anchor interval")
        ref = ANCHOR_ADAPTER.validate_python(entry["anchor_metadata"]).business_metadata()
        key = (ref["graph_id"], ref["anchor_id"])
        row = (start, end, ref)
        if key in refs and refs[key] != row:
            raise ValueError("Conflicting anchor identity")
        refs[key] = row
    boundaries = []
    if config.boundary != "characters":
        pattern = r"\n\s*\n" if config.boundary == "paragraph" else r"\n"
        boundaries = [match.end() for match in re.finditer(pattern, text)]
    result, start = [], 0
    while start < len(text):
        end = min(start + config.chunk_size, len(text))
        if end < len(text) and boundaries:
            index = bisect_right(boundaries, end) - 1
            if index >= 0 and boundaries[index] > start + config.overlap:
                end = boundaries[index]
        content = text[start:end]
        matched = [row for row in refs.values() if overlaps(start, end, row[0], row[1])]
        matched.sort(key=lambda row: (row[0], row[1], row[2]["graph_id"], row[2]["anchor_id"]))
        source_blocks = [b for b in blocks if overlaps(start, end, b["source_start"], b["source_end"])]
        pages = [page for b in source_blocks for page in (b.get("page_start"), b.get("page_end")) if page is not None]
        result.append(SequentialChunk(content, start, end, sha256_bytes(content.encode("utf-8")),
            tuple(row[2] for row in matched), tuple(b["block_id"] for b in source_blocks),
            min(pages) if pages else None, max(pages) if pages else None))
        if len(result) > max_chunks:
            raise ValueError("Sequential chunk budget exceeded")
        if end == len(text):
            break
        start = end - config.overlap
    return tuple(result)
