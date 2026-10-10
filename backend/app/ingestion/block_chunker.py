"""PDF structural grouping over a verified frozen source-map, with no IO.

Only coordinates are split/merged. Content is always sliced from canonical text.
The ChunkSet service persists and verifies these drafts before publication.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
import json
import math
import re
from typing import Any, Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ingestion.frozen_source import FrozenSource, json_bytes, sha256_bytes, verify_frozen_source

SEGMENTATION_VERSION = "pdf-block-aware-codepoints-v1"
CHUNK_METHOD = SEGMENTATION_VERSION
PARSER_PROVIDER = "mineru_api"
_OVERLAP_TYPES = frozenset({"text", "list", "footnote", "unknown"})


class BlockChunkerConfig(BaseModel):
    """The single six-field definition; omitted fields use these defaults only."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    max_chunk_chars: int = Field(default=1800, gt=0, le=60000)
    min_chunk_chars: int = Field(default=200, ge=0)
    overlap_chars: int = Field(default=0, ge=0)
    max_table_chars: int = Field(default=4000, gt=0)
    keep_table_intact: bool = True
    keep_formula_with_context: bool = True

    @model_validator(mode="after")
    def valid_lengths(self) -> BlockChunkerConfig:
        if self.min_chunk_chars > self.max_chunk_chars:
            raise ValueError("min_chunk_chars cannot exceed max_chunk_chars; provide a valid min_chunk_chars")
        if self.overlap_chars >= self.max_chunk_chars:
            raise ValueError("overlap_chars must be smaller than max_chunk_chars")
        return self

    @property
    def fingerprint(self) -> str:
        return sha256_bytes(json_bytes(self.model_dump()))


def frozen_config(value: dict, fingerprint: str, version: str) -> BlockChunkerConfig:
    """Stored contracts must be complete; only requests may fill defaults."""
    config = BlockChunkerConfig.model_validate(value)
    if (version != SEGMENTATION_VERSION or set(value) != set(BlockChunkerConfig.model_fields)
            or config.fingerprint != fingerprint):
        raise ValueError("Unknown or corrupt segmentation contract")
    return config


@dataclass(frozen=True, slots=True)
class BuiltDocumentChunk:
    chunk_index: int
    content: str = field(repr=False)
    estimated_token_count: int
    page_start: int | None
    page_end: int | None
    section_title: str | None
    chunk_type: str
    source_metadata: dict[str, Any]
    parse_run_id: str
    chunk_method: str
    content_format: str
    source_version: UUID
    source_start: int
    source_end: int
    content_sha256: str
    embedding: None = field(default=None, repr=False)

    @property
    def chunk_temp_key(self) -> str:
        return f"chunk-{self.chunk_index:06d}"


@dataclass(frozen=True, slots=True)
class BuiltChunkBlockLink:
    chunk_index: int
    chunk_temp_key: str
    block_id: str
    block_key: str | None
    block_order: int


@dataclass(frozen=True, slots=True)
class ChunkBuildResult:
    chunks: list[BuiltDocumentChunk]
    links: list[BuiltChunkBlockLink]


@dataclass(frozen=True, slots=True)
class _BlockView:
    block_id: str
    block_key: str | None
    block_type: str
    source_start: int
    source_end: int
    content_format: str
    page_start: int | None
    page_end: int | None
    section_path: tuple[str, ...]
    asset_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Interval:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _BlockFragment:
    block: _BlockView
    span: _Interval


def build_block_aware_chunks(
    source: FrozenSource, *, document_id: UUID, parse_run_id: UUID, output_prefix: str,
    config: BlockChunkerConfig | None = None,
    segmentation_version: str = SEGMENTATION_VERSION, max_chunks: int = 5000,
) -> ChunkBuildResult:
    """Consume frozen bytes and identity, never mutable DocumentBlock or a renderer."""
    if segmentation_version != SEGMENTATION_VERSION:
        raise ValueError("Unsupported PDF segmentation version")
    if type(max_chunks) is not int or max_chunks <= 0:
        raise ValueError("max_chunks must be a positive integer")
    settings = BlockChunkerConfig() if config is None else config
    if not isinstance(settings, BlockChunkerConfig):
        raise TypeError("config must be a validated BlockChunkerConfig")
    text = verify_frozen_source(
        source.canonical, source.block_map, document_id=document_id, parse_run_id=parse_run_id,
        source_version=source.source_version, output_prefix=output_prefix,
        canonical_sha256=source.canonical_sha256, block_map_sha256=source.block_map_sha256,
        character_count=source.character_count,
    )
    blocks = [
        _BlockView(**{key: entry[key] for key in (
            "block_id", "block_key", "block_type", "source_start", "source_end",
            "content_format", "page_start", "page_end")},
            section_path=tuple(entry["section_path"]), asset_keys=tuple(entry["asset_keys"]))
        for entry in json.loads(source.block_map)["blocks"]
    ]
    groups = _build_chunk_groups(text, blocks, settings, max_chunks)
    chunks, links = [], []
    for index, group in enumerate(groups):
        start, end = group[0].span.start, group[-1].span.end
        # Frozen inter-block separators belong to the previous chunk. They may
        # exceed its target length; no whitespace is synthesized or discarded.
        if index + 1 < len(groups) and end < groups[index + 1][0].span.start:
            next_start = groups[index + 1][0].span.start
            if not text[end:next_start].isspace():
                raise ValueError("Non-contiguous structural grouping")
            end = next_start
        chunk = _build_chunk(index, group, text, start, end, source.source_version, str(parse_run_id))
        chunks.append(chunk)
        for order, block in enumerate(dict.fromkeys(fragment.block for fragment in group)):
            links.append(BuiltChunkBlockLink(index, chunk.chunk_temp_key, block.block_id, block.block_key, order))
    return ChunkBuildResult(chunks, links)


def _build_chunk_groups(
    text: str, blocks: Sequence[_BlockView], config: BlockChunkerConfig, max_chunks: int,
) -> list[list[_BlockFragment]]:
    groups: list[list[_BlockFragment]] = []
    current: list[_BlockFragment] = []

    def emit(group: list[_BlockFragment]) -> None:
        if len(groups) >= max_chunks:
            raise ValueError("PDF structural chunk budget exceeded")
        groups.append(group)

    def flush() -> None:
        nonlocal current
        if current:
            emit(current)
            current = []

    def fits(fragment: _BlockFragment) -> bool:
        return fragment.span.end - current[0].span.start <= config.max_chunk_chars

    for block in blocks:
        if current and block.section_path != current[0].block.section_path:
            flush()
        span = _Interval(block.source_start, block.source_end)
        fragment = _BlockFragment(block, span)
        length = span.end - span.start
        if block.block_type == "formula" and (
            not config.keep_formula_with_context or length > config.max_chunk_chars
        ):
            flush()
            emit([fragment])
        elif block.block_type == "table":
            parts = _table_fragments(text, span, config, max_chunks)
            if len(parts) > 1:
                flush()
                for part in parts:
                    emit([_BlockFragment(block, part)])
                continue
            if current and (any(f.block.block_type != "title" for f in current) or not fits(fragment)):
                flush()
            current.append(fragment)
            flush()
        elif length > config.max_chunk_chars:
            flush()
            parts = _split_text_safely(text, span, config, max_chunks,
                                       allow_overlap=block.block_type in _OVERLAP_TYPES)
            for part in parts:
                emit([_BlockFragment(block, part)])
        else:
            if current and not fits(fragment):
                flush()
            current.append(fragment)
    flush()
    return groups


def _table_fragments(
    text: str, span: _Interval, config: BlockChunkerConfig, max_chunks: int,
) -> list[_Interval]:
    if config.keep_table_intact or span.end - span.start <= config.max_table_chars:
        return [span]
    # False means physical LF-delimited rows, not HTML parsing or logical-table repair.
    rows = [_Interval(span.start + match.start(), span.start + match.end())
            for match in re.finditer(r"[^\n]*\n|[^\n]+$", text[span.start:span.end])]
    parts: list[_Interval] = []
    start = span.start
    for row in rows:
        if (row.end - start > config.max_table_chars and row.start > start
                and text[start:row.start].strip() and text[row.start:row.end].strip()):
            parts.append(_Interval(start, row.start))
            if len(parts) >= max_chunks:
                raise ValueError("PDF structural chunk budget exceeded")
            start = row.start
    # Blank lines stay attached. An oversized physical row remains indivisible.
    parts.append(_Interval(start, span.end))
    return parts


def _split_text_safely(
    text: str, span: _Interval, config: BlockChunkerConfig, max_chunks: int,
    *, allow_overlap: bool,
) -> list[_Interval]:
    value = text[span.start:span.end]
    paragraph_ends = [span.start + m.end() for m in re.finditer(r"\n(?:[ \t]*\n)+", value)]
    whitespace_ends = [span.start + m.end() for m in re.finditer(r"\s+", value)]
    parts: list[_Interval] = []
    start = span.start
    while start < span.end:
        nonblank = start
        while nonblank < span.end and text[nonblank].isspace():
            nonblank += 1
        if nonblank == span.end:
            parts[-1] = _Interval(parts[-1].start, span.end)
            break
        # Exceptional leading whitespace cannot be emitted as a meaningless chunk.
        end = min(max(start + config.max_chunk_chars, nonblank + 1), span.end)
        if end < span.end:
            for boundaries in (paragraph_ends, whitespace_ends):
                position = bisect_right(boundaries, end) - 1
                if position >= 0 and boundaries[position] > nonblank:
                    end = boundaries[position]
                    break
            # Keep separator whitespace on the left, even when it crosses the target.
            while end < span.end and text[end].isspace():
                end += 1
        parts.append(_Interval(start, end))
        if len(parts) > max_chunks:
            raise ValueError("PDF structural chunk budget exceeded")
        start = end
    parts = _merge_small_tail(parts, config)
    return _add_safe_overlap(text, parts, config) if allow_overlap else parts


def _merge_small_tail(parts: list[_Interval], config: BlockChunkerConfig) -> list[_Interval]:
    """Only the last two base pieces of one ordinary block are eligible."""
    if len(parts) < 2 or parts[-1].end - parts[-1].start >= config.min_chunk_chars:
        return parts
    previous, tail = parts[-2:]
    if previous.end == tail.start and tail.end - previous.start <= config.max_chunk_chars:
        return [*parts[:-2], _Interval(previous.start, tail.end)]
    return parts


def _add_safe_overlap(text: str, parts: list[_Interval], config: BlockChunkerConfig) -> list[_Interval]:
    """Best effort within one ordinary block; base ends never move or repeat."""
    if not config.overlap_chars or len(parts) < 2:
        return parts
    result = [parts[0]]
    for previous, current in zip(parts, parts[1:]):
        room = max(0, config.max_chunk_chars - (current.end - current.start))
        start = max(current.start - min(room, config.overlap_chars), previous.start + 1)
        # Prefer a word boundary when one exists in the permitted suffix.
        for match in re.finditer(r"\s+", text[start:current.start]):
            start += match.end()
            break
        if start < current.start and text[start:current.start].strip():
            result.append(_Interval(start, current.end))
        else:
            result.append(current)
    return result


def _build_chunk(
    index: int, group: Sequence[_BlockFragment], text: str, start: int, end: int,
    source_version: UUID, parse_run_id: str,
) -> BuiltDocumentChunk:
    blocks = list(dict.fromkeys(fragment.block for fragment in group))
    formats = {b.content_format for b in blocks}
    content_format = next(iter(formats)) if len(formats) == 1 else "mixed"
    kinds = {b.block_type for b in blocks}
    if kinds <= {"title", "text", "list", "footnote", "unknown"}:
        kind = "text"
    elif kinds <= {"title", "table"}:
        kind = "table"
    elif kinds <= {"title", "formula"}:
        kind = "formula"
    elif kinds <= {"title", "image", "caption"}:
        kind = "image_caption"
    else:
        kind = "mixed"
    pages = [p for b in blocks for p in (b.page_start, b.page_end) if p is not None]
    section_path = list(blocks[0].section_path)
    content = text[start:end]
    metadata = dict(
        parser_provider=PARSER_PROVIDER, parse_run_id=parse_run_id,
        block_ids=[b.block_id for b in blocks],
        block_keys=list(dict.fromkeys(b.block_key for b in blocks if b.block_key is not None)),
        block_types=list(dict.fromkeys(b.block_type for b in blocks)),
        asset_keys=list(dict.fromkeys(key for b in blocks for key in b.asset_keys)),
        section_path=section_path, chunk_method=CHUNK_METHOD, content_format=content_format,
    )
    return BuiltDocumentChunk(
        index, content, max(1, math.ceil(len(content) / 4)),
        min(pages) if pages else None, max(pages) if pages else None,
        # Display-only projection in Unicode code points; metadata retains the full path.
        section_path[-1][:255] if section_path else None, kind, metadata, parse_run_id, CHUNK_METHOD,
        content_format, source_version, start, end, sha256_bytes(content.encode("utf-8")),
    )
