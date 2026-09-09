from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field, replace
import math
import re

from app.ingestion.block_chunker import BlockChunkerConfig
from app.ingestion.chunk_drafts import ChunkDraft
from app.ingestion.markdown.models import CleanMarkdownDocument, MarkdownNode


@dataclass(frozen=True, slots=True)
class _Unit:
    content: str = field(repr=False)
    nodes: tuple[MarkdownNode, ...] = field(repr=False)
    section_path: tuple[str, ...]
    section_order: int
    kind: str
    atomic: bool = False
    fragment: bool = False


def build_markdown_chunks(
    document: CleanMarkdownDocument,
    *,
    config: BlockChunkerConfig | None = None,
) -> list[ChunkDraft]:
    """Chunk validated Markdown without IO, persistence, or parser fallbacks.

    Reuse the existing character sizing/overlap configuration and len/4 token
    estimate. Reserve overlap room when packing text. Structural boundaries and
    indivisible syntax take precedence over size/minimum-size targets. MinerU
    table/formula/header flags do not override Markdown's atomic-node contract.

    Headings are included once as source content and start a new section. Lists
    and blockquotes remain whole containers, with their actual descendant
    content IDs. Long plain paragraphs split only at lexical boundaries; rich
    inline syntax and indivisible numeric expressions may remain oversized.
    """
    if not isinstance(document, CleanMarkdownDocument):
        raise TypeError("A validated CleanMarkdownDocument is required")
    settings = config or BlockChunkerConfig()
    groups = _pack(_units(document, settings), settings)
    return [
        _draft(index, _with_overlap(groups, index, settings), document)
        for index in range(len(groups))
    ]


def _units(
    document: CleanMarkdownDocument, config: BlockChunkerConfig
) -> Iterator[_Unit]:
    headings: list[tuple[int, str]] = []
    section_order = 0
    target = config.max_chunk_chars - config.overlap_chars
    for node in document.root.children:
        if node.kind == "heading":
            level = int(node.tag[1:])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, _heading_title(node)))
            section_order += 1
        content_nodes = tuple(child for child in node.walk() if child.is_content)
        if not content_nodes or not node.source.strip():
            continue
        kind = _kind(node)
        unit = _Unit(
            content=node.source,
            nodes=content_nodes,
            section_path=tuple(title for _, title in headings),
            section_order=section_order,
            kind=kind,
            atomic=kind != "text",
        )
        if (
            node.kind == "paragraph"
            and kind == "text"
            and len(node.source) > config.max_chunk_chars
            and _plain_paragraph(node)
        ):
            for fragment in _split_plain(node.source, target):
                # Original content identity and line provenance survive every
                # fragment. Text boundaries never decide anchor inheritance.
                yield replace(unit, content=fragment, fragment=True)
        else:
            yield unit


def _heading_title(node: MarkdownNode) -> str:
    def text(current: MarkdownNode) -> str:
        if current.kind in {"softbreak", "hardbreak"}:
            return " "
        if current.children:
            return "".join(text(child) for child in current.children)
        return current.content if current.kind in {"text", "code_inline", "image"} else ""

    return text(node).strip()


def _kind(node: MarkdownNode) -> str:
    if node.kind in {"bullet_list", "ordered_list"}:
        return "list"
    if node.kind == "blockquote":
        return "blockquote"
    if node.kind in {"fence", "code_block"}:
        return "code"
    if node.kind == "table":
        return "table"
    if node.kind == "paragraph":
        source = node.source.strip()
        if (source.startswith("$$") and source.endswith("$$")) or (
            source.startswith("\\[") and source.endswith("\\]")
        ):
            return "formula"
    return "text" if node.kind in {"heading", "paragraph"} else "raw"


def _plain_paragraph(node: MarkdownNode) -> bool:
    # Preserve inline Markdown and math as a whole rather than reconstructing
    # syntax after splitting. Raw source, not rendered HTML, remains evidence.
    return not any(marker in node.source for marker in ("$", "\\(", "\\[")) and all(
        child.kind in {"paragraph", "inline", "text", "softbreak"}
        for child in node.walk()
    )


def _text_atoms(source: str) -> list[str]:
    # Keep words, decimal values and units intact. Chinese sentence terminators
    # are safe boundaries too. Whitespace stays attached, preserving the source.
    return re.findall(r"[^\s。！？；]+[。！？；]*\s*|[。！？；]+\s*|\s+", source)


def _split_plain(source: str, target: int) -> Iterator[str]:
    current = ""
    for atom in _text_atoms(source):
        if current and len(current) + len(atom) > target:
            yield current
            current = ""
        current += atom
    if current:
        yield current


def _render(units: Sequence[_Unit]) -> str:
    result = ""
    previous: _Unit | None = None
    for unit in units:
        if previous is not None:
            same_fragment_origin = (
                previous.fragment and unit.fragment
                and previous.nodes[0].node_id == unit.nodes[0].node_id
            )
            if not same_fragment_origin:
                newline = "\r\n" if result.endswith("\r\n") else "\r" if result.endswith("\r") else "\n"
                if not result.endswith(newline * 2):
                    result += newline if result.endswith(newline) else newline * 2
        result += unit.content
        previous = unit
    return result


def _pack(units: Iterator[_Unit], config: BlockChunkerConfig) -> list[list[_Unit]]:
    groups: list[list[_Unit]] = []
    current: list[_Unit] = []
    target = config.max_chunk_chars - config.overlap_chars

    def flush() -> None:
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for unit in units:
        if current and current[-1].section_order != unit.section_order:
            flush()
        if unit.atomic:
            flush()
            groups.append([unit])
            continue
        if current and len(_render([*current, unit])) > target:
            flush()
        current.append(unit)
    flush()
    return groups


def _suffixes(unit: _Unit) -> Iterator[_Unit]:
    yield unit
    if unit.fragment:
        atoms = _text_atoms(unit.content)
        for start in range(1, len(atoms)):
            yield replace(unit, content="".join(atoms[start:]))


def _with_overlap(
    groups: list[list[_Unit]], index: int, config: BlockChunkerConfig
) -> list[_Unit]:
    current = groups[index]
    if not index or not config.overlap_chars:
        return current
    previous = groups[index - 1]
    if previous[-1].section_order != current[0].section_order:
        return current
    prefix: list[_Unit] = []
    # Read base groups only: overlap never propagates recursively. Every reused
    # whole node or paragraph fragment carries its real original content IDs.
    for unit in reversed(previous):
        selected = None
        for candidate in _suffixes(unit):
            proposed = [candidate, *prefix]
            if (
                len(_render(proposed)) <= config.overlap_chars
                and len(_render([*proposed, *current])) <= config.max_chunk_chars
            ):
                selected = candidate
                prefix = proposed
                break
        if selected is None or selected.content != unit.content:
            break
    return [*prefix, *current]


def _draft(
    index: int, units: list[_Unit], document: CleanMarkdownDocument
) -> ChunkDraft:
    nodes = sorted(
        {node.node_id: node for unit in units for node in unit.nodes}.values(),
        key=lambda node: node.order,
    )
    node_ids = {node.node_id for node in nodes}
    kg_refs = []
    seen_anchors: set[str] = set()
    for scope in sorted(
        document.anchor_scopes,
        key=lambda scope: (scope.start_order, scope.kg_ref.anchor_id),
    ):
        if node_ids.intersection(scope.node_ids) and scope.kg_ref.anchor_id not in seen_anchors:
            kg_refs.append(asdict(scope.kg_ref))
            seen_anchors.add(scope.kg_ref.anchor_id)
    lines = [node.source_range for node in nodes if node.source_range is not None]
    content = _render(units)
    kinds = {unit.kind for unit in units}
    section_path = list(units[-1].section_path)
    return ChunkDraft(
        chunk_index=index,
        content=content,
        token_count=max(1, math.ceil(len(content) / 4)),
        page_start=None,
        page_end=None,
        section_title=section_path[-1] if section_path else None,
        chunk_type=next(iter(kinds)) if len(kinds) == 1 else "mixed",
        chunk_method="markdown_ast",
        content_format="markdown",
        source_metadata={
            "parser_provider": "markdown_native",
            "parser_version": document.parser_version,
            "chunk_method": "markdown_ast",
            "content_format": "markdown",
            "section_path": section_path,
            "source_range": {
                "kind": "markdown_ast",
                # Fragments conservatively retain their original node's lines.
                # The envelope may include gaps; ast_node_ids is authoritative.
                "start_line": min(line.start_line for line in lines) if lines else None,
                "end_line": max(line.end_line for line in lines) if lines else None,
                "ast_node_ids": [node.node_id for node in nodes],
            },
            "kg_refs": kg_refs,
        },
    )
