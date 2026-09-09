from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from app.graph.models import KGRef


PARSER_VERSION = "markdown-it-py/4.2.0"


@dataclass(frozen=True, slots=True)
class SourceLineRange:
    """One-based, inclusive original source lines; never a scope identity."""

    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class MarkdownNode:
    """Immutable token-tree projection, retaining syntax and original Markdown.

    IDs and order are local to one Source, assigned in parser preorder before
    controls are removed. Only is_content nodes participate in inheritance:
    block paragraphs/headings, atomic tables/code/HTML, and thematic breaks.
    Inline descendants retain structure without duplicating their block's unit.
    Root, lists and blockquotes are containers, never shared content identities.
    """

    node_id: str
    order: int
    kind: str
    source_range: SourceLineRange | None
    source: str = field(repr=False)
    content: str = field(repr=False)
    is_content: bool
    tag: str = ""
    markup: str = ""
    info: str = ""
    attrs: tuple[tuple[str, str | int | float], ...] = ()
    children: tuple[MarkdownNode, ...] = ()

    def walk(self) -> Iterator[MarkdownNode]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True, slots=True)
class MarkdownAST:
    """Unvalidated intermediate AST. Only CleanMarkdownDocument is downstream input."""

    root: MarkdownNode = field(repr=False)
    source_lines: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class AnchorScope:
    kg_ref: KGRef
    start_order: int
    end_order: int
    node_ids: tuple[str, ...]
    source_range: SourceLineRange
    container_id: str


@dataclass(frozen=True, slots=True)
class CleanMarkdownDocument:
    root: MarkdownNode = field(repr=False)
    anchor_scopes: tuple[AnchorScope, ...]
    clean_source: str = field(repr=False)
    parser_version: str = PARSER_VERSION

    @property
    def content_nodes(self) -> tuple[MarkdownNode, ...]:
        return tuple(node for node in self.root.walk() if node.is_content)
