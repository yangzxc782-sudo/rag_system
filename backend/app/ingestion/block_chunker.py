from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.ingestion.mineru.normalizer import NormalizedDocumentBlock

CHUNK_METHOD = "mineru_block_merge"
PARSER_PROVIDER = "mineru_api"
BlockContentRenderer = Callable[[Any], tuple[str, str]]


@dataclass(frozen=True, slots=True)
class BlockChunkerConfig:
    max_chunk_chars: int = 1800
    min_chunk_chars: int = 200
    overlap_chars: int = 0
    max_table_chars: int = 4000
    keep_table_intact: bool = True
    keep_formula_with_context: bool = True
    include_headers_footers: bool = False

    def __post_init__(self) -> None:
        if self.max_chunk_chars <= 0:
            raise ValueError("max_chunk_chars must be positive")
        if self.min_chunk_chars < 0:
            raise ValueError("min_chunk_chars cannot be negative")
        if self.min_chunk_chars > self.max_chunk_chars:
            raise ValueError("min_chunk_chars cannot exceed max_chunk_chars")
        if self.overlap_chars < 0:
            raise ValueError("overlap_chars cannot be negative")
        if self.overlap_chars >= self.max_chunk_chars:
            raise ValueError("overlap_chars must be smaller than max_chunk_chars")
        if self.max_table_chars <= 0:
            raise ValueError("max_table_chars must be positive")


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
    embedding: None = field(default=None, repr=False)

    @property
    def chunk_temp_key(self) -> str:
        return f"chunk-{self.chunk_index:06d}"


@dataclass(frozen=True, slots=True)
class BuiltChunkBlockLink:
    chunk_index: int
    chunk_temp_key: str
    block_id: str | None
    block_key: str | None
    block_order: int


@dataclass(frozen=True, slots=True)
class ChunkBuildResult:
    chunks: list[BuiltDocumentChunk]
    links: list[BuiltChunkBlockLink]


@dataclass(frozen=True, slots=True)
class _BlockView:
    block_id: str | None
    block_key: str | None
    block_index: int
    block_type: str
    content: str = field(repr=False)
    content_format: str
    page_start: int | None
    page_end: int | None
    section_path: list[str]
    asset_keys: list[str]


@dataclass(frozen=True, slots=True)
class _BlockFragment:
    block: _BlockView
    content: str = field(repr=False)


def build_block_aware_chunks(
    blocks: Iterable[NormalizedDocumentBlock | Any],
    *,
    parse_run_id: str,
    config: BlockChunkerConfig | None = None,
    renderer: BlockContentRenderer | None = None,
) -> ChunkBuildResult:
    settings = config or BlockChunkerConfig()
    parse_run_value = str(parse_run_id)
    if not parse_run_value.strip():
        raise ValueError("parse_run_id is required")

    views = _prepare_block_views(blocks, settings, renderer)
    groups = _build_chunk_groups(views, settings)
    chunks: list[BuiltDocumentChunk] = []
    links: list[BuiltChunkBlockLink] = []
    for chunk_index, group in enumerate(groups):
        chunk = _build_chunk(chunk_index, group, parse_run_value)
        chunks.append(chunk)
        links.extend(_build_links(chunk, group))
    return ChunkBuildResult(chunks=chunks, links=links)


def _prepare_block_views(
    blocks: Iterable[NormalizedDocumentBlock | Any],
    config: BlockChunkerConfig,
    renderer: BlockContentRenderer | None = None,
) -> list[_BlockView]:
    views: list[_BlockView] = []
    active_section_path: list[str] = []
    for fallback_index, block in enumerate(blocks):
        view = _to_block_view(block, fallback_index, renderer)
        if (
            view.block_type in {"header", "footer"}
            and not config.include_headers_footers
        ):
            continue
        if not view.content.strip():
            continue

        if view.section_path:
            active_section_path = list(view.section_path)
        elif view.block_type == "title":
            active_section_path = [view.content.strip()]
            view = replace(view, section_path=list(active_section_path))
        elif active_section_path:
            view = replace(view, section_path=list(active_section_path))
        views.append(view)
    return views


def _build_chunk_groups(
    blocks: Sequence[_BlockView],
    config: BlockChunkerConfig,
) -> list[list[_BlockFragment]]:
    groups: list[list[_BlockFragment]] = []
    current: list[_BlockFragment] = []

    def flush() -> None:
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for block in blocks:
        if current and block.section_path != current[0].block.section_path:
            flush()

        if (
            block.block_type == "formula"
            and (
                not config.keep_formula_with_context
                or len(block.content) > config.max_chunk_chars
            )
        ):
            flush()
            groups.append(
                [_BlockFragment(block=block, content=block.content)]
            )
            continue

        if block.block_type == "table":
            table_fragments = _table_fragments(block, config)
            if len(table_fragments) > 1:
                flush()
                groups.extend([[fragment] for fragment in table_fragments])
                continue

            fragment = table_fragments[0]
            if current and not _only_title_fragments(current):
                flush()
            if current and not _fits(current, fragment.content, config):
                flush()
            current.append(fragment)
            flush()
            continue

        if len(block.content) > config.max_chunk_chars:
            flush()
            groups.extend(
                [
                    [_BlockFragment(block=block, content=part)]
                    for part in _split_text_safely(block.content, config)
                ]
            )
            continue

        fragment = _BlockFragment(block=block, content=block.content)
        if current and not _fits(current, fragment.content, config):
            flush()
        current.append(fragment)

    flush()
    return groups


def _table_fragments(
    block: _BlockView,
    config: BlockChunkerConfig,
) -> list[_BlockFragment]:
    if config.keep_table_intact or len(block.content) <= config.max_table_chars:
        return [_BlockFragment(block=block, content=block.content)]
    parts = _split_lines(block.content, config.max_table_chars)
    return [_BlockFragment(block=block, content=part) for part in parts]


def _build_chunk(
    chunk_index: int,
    group: Sequence[_BlockFragment],
    parse_run_id: str,
) -> BuiltDocumentChunk:
    content = "\n\n".join(fragment.content.strip() for fragment in group).strip()
    content_format = _chunk_content_format(group)
    section_path = list(group[0].block.section_path)
    block_ids = _deduplicate(
        fragment.block.block_id
        for fragment in group
        if fragment.block.block_id is not None
    )
    block_keys = _deduplicate(
        fragment.block.block_key
        for fragment in group
        if fragment.block.block_key is not None
    )
    block_types = _deduplicate(fragment.block.block_type for fragment in group)
    asset_keys = _deduplicate(
        asset_key
        for fragment in group
        for asset_key in fragment.block.asset_keys
    )
    source_metadata = {
        "parser_provider": PARSER_PROVIDER,
        "parse_run_id": parse_run_id,
        "block_ids": block_ids,
        "block_keys": block_keys,
        "block_types": block_types,
        "asset_keys": asset_keys,
        "section_path": section_path,
        "chunk_method": CHUNK_METHOD,
        "content_format": content_format,
    }
    pages = [
        page
        for fragment in group
        for page in (fragment.block.page_start, fragment.block.page_end)
        if page is not None
    ]
    return BuiltDocumentChunk(
        chunk_index=chunk_index,
        content=content,
        estimated_token_count=max(1, math.ceil(len(content) / 4)),
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
        section_title=section_path[-1] if section_path else None,
        chunk_type=_chunk_type(group),
        source_metadata=source_metadata,
        parse_run_id=parse_run_id,
        chunk_method=CHUNK_METHOD,
        content_format=content_format,
    )


def _build_links(
    chunk: BuiltDocumentChunk,
    group: Sequence[_BlockFragment],
) -> list[BuiltChunkBlockLink]:
    links: list[BuiltChunkBlockLink] = []
    seen: set[tuple[str | None, str | None, int]] = set()
    for fragment in group:
        identity = (
            fragment.block.block_id,
            fragment.block.block_key,
            fragment.block.block_index,
        )
        if identity in seen:
            continue
        seen.add(identity)
        links.append(
            BuiltChunkBlockLink(
                chunk_index=chunk.chunk_index,
                chunk_temp_key=chunk.chunk_temp_key,
                block_id=fragment.block.block_id,
                block_key=fragment.block.block_key,
                block_order=len(links),
            )
        )
    return links


def _to_block_view(
    block: Any, fallback_index: int, renderer: BlockContentRenderer | None = None,
) -> _BlockView:
    block_type = str(_value(block, "block_type") or "unknown").strip().lower()
    source_metadata = _mapping(_value(block, "source_metadata"))
    text = _optional_text(_value(block, "text"))
    markdown = _optional_text(_value(block, "markdown"))
    html = _optional_text(_value(block, "html"))
    latex = _optional_text(_value(block, "latex"))
    caption = _optional_text(_value(block, "caption"))
    content, content_format = _render_block_content(
        block_type=block_type,
        text=text,
        markdown=markdown,
        html=html,
        latex=latex,
        caption=caption,
    )
    if renderer is not None:
        content, content_format = renderer(block)
    block_id = _optional_text(_value(block, "id"))
    if block_id is None:
        block_id = _optional_text(source_metadata.get("document_block_id"))
    block_key = _optional_text(_value(block, "block_key"))
    section_path = _string_list(_value(block, "section_path"))
    if not section_path:
        section_path = _string_list(source_metadata.get("section_path"))
    asset_keys = _string_list(_value(block, "asset_keys"))
    if not asset_keys:
        asset_keys = _string_list(source_metadata.get("asset_keys"))
    return _BlockView(
        block_id=block_id,
        block_key=block_key,
        block_index=_optional_int(_value(block, "block_index")) or fallback_index,
        block_type=block_type,
        content=content,
        content_format=content_format,
        page_start=_optional_int(_value(block, "page_start")),
        page_end=_optional_int(_value(block, "page_end")),
        section_path=section_path,
        asset_keys=_deduplicate(asset_keys),
    )


def _render_block_content(
    *,
    block_type: str,
    text: str | None,
    markdown: str | None,
    html: str | None,
    latex: str | None,
    caption: str | None,
) -> tuple[str, str]:
    if block_type == "table":
        if markdown:
            return markdown, "markdown"
        if html:
            return html, "mixed"
        return text or "", "plain_text"

    if block_type == "formula":
        rendered_formula = f"$$\n{latex}\n$$" if latex else ""
        parts = _deduplicate(part for part in (text, rendered_formula) if part)
        if rendered_formula and text:
            return "\n\n".join(parts), "mixed"
        if rendered_formula:
            return rendered_formula, "markdown"
        return text or "", "plain_text"

    if block_type in {"image", "caption"}:
        parts = _deduplicate(part for part in (caption, text) if part)
        return "\n\n".join(parts), "plain_text"

    if markdown and text and markdown != text:
        return f"{text}\n\n{markdown}", "mixed"
    if markdown:
        return markdown, "markdown"
    return text or caption or "", "plain_text"


def _split_text_safely(
    content: str,
    config: BlockChunkerConfig,
) -> list[str]:
    paragraphs = [part.strip() for part in content.split("\n\n") if part.strip()]
    units = [
        piece
        for paragraph in paragraphs
        for piece in _split_oversized_text(paragraph, config.max_chunk_chars)
    ]
    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}".strip() if current else unit
        if current and len(candidate) > config.max_chunk_chars:
            chunks.append(current)
            current = unit
        else:
            current = candidate
    if current:
        chunks.append(current)
    chunks = _merge_small_tail(chunks, config)
    return _add_safe_overlap(chunks, config)


def _split_oversized_text(content: str, max_chars: int) -> list[str]:
    remaining = content.strip()
    parts: list[str] = []
    while len(remaining) > max_chars:
        cut = remaining.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def _split_lines(content: str, max_chars: int) -> list[str]:
    lines = [line for line in content.splitlines() if line.strip()]
    parts: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}".strip() if current else line
        if current and len(candidate) > max_chars:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts or [content]


def _merge_small_tail(
    chunks: list[str],
    config: BlockChunkerConfig,
) -> list[str]:
    if len(chunks) < 2 or len(chunks[-1]) >= config.min_chunk_chars:
        return chunks
    merged = f"{chunks[-2]}\n\n{chunks[-1]}"
    if len(merged) <= config.max_chunk_chars:
        return [*chunks[:-2], merged]
    return chunks


def _add_safe_overlap(
    chunks: list[str],
    config: BlockChunkerConfig,
) -> list[str]:
    if config.overlap_chars == 0 or len(chunks) < 2:
        return chunks
    overlapped = [chunks[0]]
    for previous, current in zip(chunks, chunks[1:]):
        suffix = previous[-config.overlap_chars :].lstrip()
        first_space = suffix.find(" ")
        if first_space >= 0:
            suffix = suffix[first_space + 1 :].strip()
        candidate = f"{suffix}\n\n{current}".strip() if suffix else current
        overlapped.append(
            candidate if len(candidate) <= config.max_chunk_chars else current
        )
    return overlapped


def _fits(
    current: Sequence[_BlockFragment],
    next_content: str,
    config: BlockChunkerConfig,
) -> bool:
    current_length = sum(len(fragment.content.strip()) for fragment in current)
    separators = 2 * len(current)
    return current_length + separators + len(next_content.strip()) <= (
        config.max_chunk_chars
    )


def _only_title_fragments(group: Sequence[_BlockFragment]) -> bool:
    return all(fragment.block.block_type == "title" for fragment in group)


def _chunk_content_format(group: Sequence[_BlockFragment]) -> str:
    formats = {fragment.block.content_format for fragment in group}
    return next(iter(formats)) if len(formats) == 1 else "mixed"


def _chunk_type(group: Sequence[_BlockFragment]) -> str:
    types = {fragment.block.block_type for fragment in group}
    if types <= {"title", "text", "list", "footnote", "unknown"}:
        return "text"
    if types <= {"title", "table"}:
        return "table"
    if types <= {"title", "formula"}:
        return "formula"
    if types <= {"title", "image", "caption"}:
        return "image_caption"
    return "mixed"


def _value(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [text for item in value if (text := _optional_text(item))]


def _deduplicate(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _optional_text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, bytes, bytearray)):
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
