from __future__ import annotations

from itertools import count
import re

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

from app.core.errors import DOCUMENT_MARKDOWN_INVALID, BusinessError
from app.ingestion.markdown.anchors import parse_anchors
from app.ingestion.markdown.models import (
    CleanMarkdownDocument,
    MarkdownAST,
    MarkdownNode,
    SourceLineRange,
)


_CONTENT_KINDS = frozenset(
    {"heading", "paragraph", "table", "fence", "code_block", "html_block", "hr"}
)


def parse_markdown(source: str | bytes) -> CleanMarkdownDocument:
    """Validate a Canonical Source and return only clean AST plus anchor scopes."""
    return parse_anchors(parse_markdown_ast(source))


def parse_markdown_ast(source: str | bytes) -> MarkdownAST:
    """Build the intermediate tree; callers must run parse_anchors before use.

    CommonMark plus the built-in table rule is deliberately fixed. Raw formulas
    remain paragraphs. No rendering, IO, parser fallback or chunking occurs here.
    """
    decoded: str | None = None
    try:
        if isinstance(source, bytes):
            decoded = source.decode("utf-8-sig")
        else:
            source.encode("utf-8")  # Reject unpaired surrogates in Python strings.
            decoded = source.removeprefix("\ufeff")
    except UnicodeError:
        pass
    if decoded is None:
        # Outside the except block: no original bytes remain in exception context.
        raise BusinessError(
            DOCUMENT_MARKDOWN_INVALID,
            "Markdown 编码无效。",
            detail={"reason": "MARKDOWN_ENCODING_INVALID", "line": 1},
            status_code=400,
        )

    # Match the parser's CR/LF line semantics while retaining original line endings.
    # Unicode separators within paragraphs must not shift provenance line numbers.
    lines = tuple(re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", decoded)[:-1])
    tree = SyntaxTreeNode(MarkdownIt("commonmark").enable("table").parse(decoded))
    orders = count()

    def project(node: SyntaxTreeNode, *, inside_content: bool = False) -> MarkdownNode:
        order = next(orders)
        line_map = (0, len(lines)) if node.is_root and lines else None
        if not node.is_root:
            line_map = node.map
        source_range = (
            SourceLineRange(line_map[0] + 1, line_map[1]) if line_map else None
        )
        is_content = node.type in _CONTENT_KINDS and not inside_content
        return MarkdownNode(
            node_id=f"n{order:06d}",
            order=order,
            kind=node.type,
            source_range=source_range,
            source="".join(lines[line_map[0]:line_map[1]]) if line_map else "",
            content="" if node.is_root else node.content,
            is_content=is_content,
            tag="" if node.is_root else node.tag,
            markup="" if node.is_root else node.markup,
            info="" if node.is_root else node.info,
            attrs=() if node.is_root else tuple(sorted(node.attrs.items())),
            children=tuple(
                project(child, inside_content=inside_content or is_content)
                for child in node.children
            ),
        )

    return MarkdownAST(root=project(tree), source_lines=lines)
