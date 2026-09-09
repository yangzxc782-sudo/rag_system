from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re

from app.core.errors import DOCUMENT_MARKDOWN_INVALID, BusinessError
from app.graph.models import KGRef
from app.ingestion.markdown.models import (
    AnchorScope,
    CleanMarkdownDocument,
    MarkdownAST,
    MarkdownNode,
    SourceLineRange,
)


_MARKERS = ("kg-anchor-start", "kg-anchor-end")
_CONTAINERS = frozenset({"root", "blockquote", "list_item"})
_CONTROL = re.compile(
    r"[ ]{0,3}<!-- kg-anchor-(start|end)[ \t]*\n(.*?)^[ ]{0,3}-->[ \t]*\n?",
    re.DOTALL | re.MULTILINE,
)
_START_FIELDS = frozenset({"anchor_id", "graph_id", "anchor_type", "table_ref"})


@dataclass(frozen=True, slots=True)
class _Control:
    node: MarkdownNode
    kind: str
    fields: dict[str, str | None]


@dataclass(slots=True)
class _OpenAnchor:
    control: _Control
    kg_ref: KGRef
    container_id: str
    node_ids: list[str] = field(default_factory=list)


def _invalid(reason: str, line: int) -> BusinessError:
    # Metadata values are untrusted too; omit the optional anchor_id from errors.
    return BusinessError(
        DOCUMENT_MARKDOWN_INVALID,
        "Markdown Anchor 格式无效。",
        detail={"reason": reason, "line": line},
        status_code=400,
    )


def _has_marker(value: str) -> bool:
    return any(marker in value for marker in _MARKERS)


def _line(node: MarkdownNode) -> int:
    return node.source_range.start_line if node.source_range else 1


def _scalar(value: str, line: int) -> str | None:
    raw = value.strip()
    if not raw or raw == "null":
        return None
    if raw.startswith('"'):
        decoded = None
        try:
            decoded = json.loads(raw)
        except ValueError:
            pass
        if not isinstance(decoded, str):
            raise _invalid("ANCHOR_METADATA_INVALID", line)
        value = decoded
    else:
        # No YAML structures, tags, aliases, quoting or block scalars.
        if raw[0] in "[]{}|>&*!'" or raw.startswith(("- ", "? ", ": ")):
            raise _invalid("ANCHOR_METADATA_INVALID", line)
        value = raw
    if any(
        ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF or char in "\x85\u2028\u2029"
        for char in value
    ):
        raise _invalid("ANCHOR_METADATA_INVALID", line)
    return value


def _metadata(body: str, kind: str, start_line: int) -> dict[str, str | None]:
    allowed = _START_FIELDS if kind == "start" else {"anchor_id"}
    values: dict[str, str | None] = {}
    entries = body.removesuffix("\n").split("\n") if body else ()
    for offset, entry in enumerate(entries, start=1):
        line = start_line + offset
        key, separator, raw = entry.partition(":")
        if not separator or key not in allowed or key in values:
            raise _invalid("ANCHOR_METADATA_INVALID", line)
        values[key] = _scalar(raw, line)
    required = ("anchor_id", "graph_id", "anchor_type") if kind == "start" else ("anchor_id",)
    if any(not values.get(key) or not values[key].strip() for key in required):
        raise _invalid("ANCHOR_REQUIRED_FIELD_MISSING", start_line)
    if values.get("anchor_type") == "table" and not (values.get("table_ref") or "").strip():
        raise _invalid("ANCHOR_TABLE_REF_REQUIRED", start_line)
    return values


def _controls(ast: MarkdownAST) -> tuple[dict[str, _Control], set[int]]:
    controls: dict[str, _Control] = {}
    removed_lines: set[int] = set()
    # First validate every reserved occurrence, including definitions not emitted
    # as content tokens. Line membership here validates placement/removal ONLY;
    # the stack below computes coverage by visiting content node identities.
    for node in ast.root.walk():
        if node.kind != "html_block" or not _has_marker(node.content):
            continue
        match = _CONTROL.fullmatch(node.content)
        if match is None or sum(node.content.count(marker) for marker in _MARKERS) != 1:
            raise _invalid("ANCHOR_CONTROL_POSITION_INVALID", _line(node))
        kind, body = match.groups()
        controls[node.node_id] = _Control(node, kind, {})
        assert node.source_range is not None
        removed_lines.update(range(node.source_range.start_line, node.source_range.end_line + 1))

    for line, source in enumerate(ast.source_lines, start=1):
        if _has_marker(source) and line not in removed_lines:
            raise _invalid("ANCHOR_CONTROL_POSITION_INVALID", line)
    for node in ast.root.walk():
        if node.node_id in controls:
            continue
        # Also reject reserved text produced by inline entity/escape decoding.
        if any(_has_marker(value) for value in (node.content, node.info, *(str(v) for _, v in node.attrs))):
            raise _invalid("ANCHOR_CONTROL_POSITION_INVALID", _line(node))
    for node_id, control in tuple(controls.items()):
        match = _CONTROL.fullmatch(control.node.content)
        assert match is not None
        controls[node_id] = replace(control, fields=_metadata(match[2], control.kind, _line(control.node)))
    return controls, removed_lines


def parse_anchors(ast: MarkdownAST) -> CleanMarkdownDocument:
    """Stack-pair controls in their own AST containers, then remove their text.

    Scope boundaries use parser preorder. Visiting a content unit appends its ID
    to every active scope, naturally supporting nested scopes without offsets.
    No partial result is returned if any control is invalid.
    """
    controls, removed_lines = _controls(ast)
    stack: list[_OpenAnchor] = []
    seen: set[str] = set()
    scopes: list[AnchorScope] = []

    def visit(node: MarkdownNode, parent: MarkdownNode | None) -> None:
        control = controls.get(node.node_id)
        if control is not None:
            line = _line(node)
            if parent is None or parent.kind not in _CONTAINERS:
                raise _invalid("ANCHOR_CONTROL_POSITION_INVALID", line)
            anchor_id = control.fields["anchor_id"]
            assert isinstance(anchor_id, str)
            if control.kind == "start":
                if anchor_id in seen:
                    raise _invalid("ANCHOR_DUPLICATE_ID", line)
                seen.add(anchor_id)
                graph_id, anchor_type = control.fields["graph_id"], control.fields["anchor_type"]
                assert isinstance(graph_id, str) and isinstance(anchor_type, str)
                stack.append(_OpenAnchor(
                    control=control,
                    kg_ref=KGRef(anchor_id, graph_id, anchor_type, control.fields.get("table_ref")),
                    container_id=parent.node_id,
                ))
            else:
                if not stack:
                    raise _invalid("ANCHOR_END_WITHOUT_START", line)
                opened = stack[-1]
                if opened.kg_ref.anchor_id != anchor_id:
                    reason = "ANCHOR_CROSSING" if any(item.kg_ref.anchor_id == anchor_id for item in stack) else "ANCHOR_ID_MISMATCH"
                    raise _invalid(reason, line)
                if opened.container_id != parent.node_id:
                    raise _invalid("ANCHOR_CONTAINER_MISMATCH", line)
                assert node.source_range is not None
                scopes.append(AnchorScope(
                    kg_ref=opened.kg_ref,
                    start_order=opened.control.node.order,
                    end_order=node.order,
                    node_ids=tuple(opened.node_ids),
                    source_range=SourceLineRange(_line(opened.control.node), node.source_range.end_line),
                    container_id=parent.node_id,
                ))
                stack.pop()
            return
        if node.is_content:
            for opened in stack:
                opened.node_ids.append(node.node_id)
        for child in node.children:
            visit(child, node)

    visit(ast.root, None)
    if stack:
        raise _invalid("ANCHOR_UNCLOSED", _line(stack[-1].control.node))

    def clean(node: MarkdownNode) -> MarkdownNode:
        source = node.source
        if node.source_range is not None:
            # Parent raw slices also contain controls. Remove complete validated
            # control lines from every slice, not just from html_block children.
            source = "".join(
                line for index, line in enumerate(
                    re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", source)[:-1],
                    start=node.source_range.start_line,
                ) if index not in removed_lines
            )
        return replace(node, source=source, children=tuple(
            clean(child) for child in node.children if child.node_id not in controls
        ))

    root = clean(ast.root)
    return CleanMarkdownDocument(
        root=root,
        anchor_scopes=tuple(sorted(scopes, key=lambda scope: (scope.start_order, scope.kg_ref.anchor_id))),
        clean_source="".join(line for index, line in enumerate(ast.source_lines, start=1) if index not in removed_lines),
    )
