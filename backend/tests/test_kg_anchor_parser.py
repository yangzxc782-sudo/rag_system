from __future__ import annotations

from dataclasses import asdict, replace
import json

import pytest

from app.core.errors import BusinessError


def parse(source: str | bytes):
    from app.ingestion.markdown.parser import parse_markdown

    return parse_markdown(source)


def start(anchor_id="A", *, anchor_type="section", table_line=""):
    return (
        "<!-- kg-anchor-start\n"
        f"anchor_id: {anchor_id}\ngraph_id: G::2026\nanchor_type: {anchor_type}\n"
        f"{table_line}-->\n"
    )


def end(anchor_id="A"):
    return f"<!-- kg-anchor-end\nanchor_id: {anchor_id}\n-->\n"


def assert_invalid(source, reason, *, line=None):
    with pytest.raises(BusinessError) as caught:
        parse(source)
    error = caught.value
    assert error.code == "DOCUMENT_MARKDOWN_INVALID"
    assert error.status_code == 400
    assert error.detail["reason"] == reason
    assert set(error.detail) <= {"reason", "line", "anchor_id"}
    assert isinstance(error.detail["line"], int)
    if line is not None:
        assert error.detail["line"] == line
    assert error.__context__ is None
    return error


def test_valid_canonical_table_anchor_covers_only_body_nodes():
    anchor_id = "KG-20260826-001::T-P8-1"
    table = "| Si | wt.% |\n| --- | --- |\n| 6.50 | 7.50 |\n"
    source = "Before\n\n" + start(anchor_id, anchor_type="table", table_line="table_ref: T-P8-1\n") + table + end(anchor_id) + "\nAfter\n"
    result = parse(source)
    before, body, after = result.content_nodes
    scope, = result.anchor_scopes
    assert asdict(scope.kg_ref) == {
        "anchor_id": anchor_id, "graph_id": "G::2026", "anchor_type": "table", "table_ref": "T-P8-1",
    }
    assert scope.node_ids == (body.node_id,)
    assert scope.start_order < body.order < scope.end_order
    assert before.order < scope.start_order < scope.end_order < after.order
    assert (scope.source_range.start_line, scope.source_range.end_line) == (3, 14)
    assert body.source == table


@pytest.mark.parametrize("table_line", ["", "table_ref:\n", "table_ref: null\n"])
def test_non_table_anchor_allows_none(table_line):
    scope, = parse(start(table_line=table_line) + "Body\n" + end()).anchor_scopes
    assert scope.kg_ref.table_ref is None


@pytest.mark.parametrize("table_line", ["", "table_ref:\n", "table_ref: null\n", 'table_ref: ""\n', 'table_ref: "  "\n'])
def test_table_anchor_requires_nonempty_table_ref(table_line):
    assert_invalid(start(anchor_type="table", table_line=table_line) + "Body\n" + end(), "ANCHOR_TABLE_REF_REQUIRED")


def test_metadata_splits_first_colon_and_accepts_json_strings():
    source = (
        '<!-- kg-anchor-start\nanchor_id: "G::A"\ngraph_id: G::2026\n'
        'anchor_type: "section"\ntable_ref: "表: A\\u0020B"\n-->\n'
        'Body\n<!-- kg-anchor-end\nanchor_id: "G::A"\n-->\n'
    )
    ref = parse(source).anchor_scopes[0].kg_ref
    assert ref.anchor_id == "G::A"
    assert ref.graph_id == "G::2026"
    assert ref.table_ref == "表: A B"


def test_quoted_null_is_a_string_not_null():
    ref = parse(start(table_line='table_ref: "null"\n') + "Body\n" + end()).anchor_scopes[0].kg_ref
    assert ref.table_ref == "null"


def test_unclosed_anchor():
    assert_invalid(start() + "Body\n", "ANCHOR_UNCLOSED", line=1)


def test_end_without_start():
    assert_invalid(end(), "ANCHOR_END_WITHOUT_START", line=1)


def test_mismatched_id():
    assert_invalid(start() + "Body\n" + end("B"), "ANCHOR_ID_MISMATCH", line=7)


@pytest.mark.parametrize("body", [start() + end() + start() + end(), start() + start() + end() + end()])
def test_duplicate_id_is_source_wide(body):
    assert_invalid(body, "ANCHOR_DUPLICATE_ID")


def test_nested_scopes_include_inner_content_and_are_sorted_by_start():
    result = parse(start() + "Outer 1\n\n" + start("B") + "Inner\n" + end("B") + "\nOuter 2\n" + end())
    outer, inner = result.anchor_scopes
    assert [scope.kg_ref.anchor_id for scope in result.anchor_scopes] == ["A", "B"]
    assert outer.node_ids == tuple(node.node_id for node in result.content_nodes)
    assert inner.node_ids == (result.content_nodes[1].node_id,)
    assert outer.start_order < inner.start_order < inner.end_order < outer.end_order
    assert result == parse(start() + "Outer 1\n\n" + start("B") + "Inner\n" + end("B") + "\nOuter 2\n" + end())


def test_crossing_scopes_are_rejected():
    assert_invalid(start() + start("B") + "Body\n" + end() + end("B"), "ANCHOR_CROSSING")


@pytest.mark.parametrize("field", ["anchor_id", "graph_id", "anchor_type"])
def test_missing_required_field(field):
    source = "\n".join(line for line in start().split("\n") if not line.startswith(field + ":"))
    assert_invalid(source + "Body\n" + end(), "ANCHOR_REQUIRED_FIELD_MISSING")


@pytest.mark.parametrize("value", ["", "null", '""', '"  "'])
def test_required_empty_scalar_is_rejected(value):
    assert_invalid(start().replace("anchor_id: A", "anchor_id: " + value) + end(), "ANCHOR_REQUIRED_FIELD_MISSING")


@pytest.mark.parametrize("line", [
    "malformed", ": value", "unknown: value", "graph_id: another",
    "table_ref: [A, B]", "table_ref: {key: value}", "table_ref: |", "table_ref: >-",
    "table_ref: &alias", "table_ref: *alias", "table_ref: !tag A", "table_ref: 'yaml quoted'",
    "  table_ref: nested", "table_ref:\n  nested: value", 'table_ref: "unterminated',
    'table_ref: "line\\nline"', "table_ref: - item", "- table_ref: A",
])
def test_bad_duplicate_unknown_or_structured_metadata_is_rejected(line):
    assert_invalid(start(table_line=line + "\n") + "Body\n" + end(), "ANCHOR_METADATA_INVALID")


def test_end_only_accepts_anchor_id():
    assert_invalid(start() + "Body\n" + end().replace("-->\n", "graph_id: G\n-->\n"), "ANCHOR_METADATA_INVALID")


@pytest.mark.parametrize("source", [
    "<!-- kg-anchor-start\n-->\n" + end(),
    start() + "<!-- kg-anchor-end\n-->\n",
])
def test_control_without_metadata_reports_missing_required_fields(source):
    assert_invalid(source, "ANCHOR_REQUIRED_FIELD_MISSING")


@pytest.mark.parametrize("scalar", ['"first\\u2028second"', '"first\\u0085second"'])
def test_json_string_cannot_hide_a_multiline_scalar(scalar):
    assert_invalid(start(table_line="table_ref: " + scalar + "\n") + end(), "ANCHOR_METADATA_INVALID")


@pytest.mark.parametrize("source", [
    "Example `kg-anchor-start` here\n", "Example kg-anchor-end here\n",
    "```markdown\n" + start() + "```\n", "    kg-anchor-start\n",
    "<!-- kg-anchor-start\nanchor_id: A\n", "<div>\n" + start() + "</div>\n",
    "<!-- ordinary kg-anchor-start comment -->\n", "Text " + start(),
    start().replace("-->\n", "--> trailing text\n"),
    "<!-- kg-anchor-start anchor_id: A -->\n",
    "[example]: /kg-anchor-start\n", "![kg-anchor-end](image.png)\n",
    "<!-- kg-anchor-start\nanchor_id: A\n--> <!-- ordinary -->\n",
    "kg&#45;anchor-start\n", "[fake](https://example.test/kg-anchor-end)\n",
])
def test_reserved_markers_in_non_control_positions_fail_closed(source):
    assert_invalid(source, "ANCHOR_CONTROL_POSITION_INVALID")


def quote(source):
    return "".join("> " + line for line in source.splitlines(keepends=True))


def test_pair_inside_one_blockquote_container():
    source = quote(start() + "Quoted 0.05\n\n" + end())
    result = parse(source)
    scope, = result.anchor_scopes
    assert result.root.children[0].kind == "blockquote"
    assert scope.container_id == result.root.children[0].node_id
    assert scope.node_ids == (result.content_nodes[0].node_id,)
    assert result.content_nodes[0].source == "> Quoted 0.05\n"


def test_pair_inside_one_list_item_container():
    source = "- Item\n\n" + "".join("  " + line for line in (start() + "Inside\n\n" + end()).splitlines(keepends=True))
    result = parse(source)
    assert [node.kind for node in result.root.children] == ["bullet_list"]
    assert result.anchor_scopes[0].node_ids == (result.content_nodes[1].node_id,)


@pytest.mark.parametrize("source", [
    start() + quote("Body\n\n" + end()),
    quote(start() + "Body\n") + "\n" + end(),
    quote(start()) + "\nOutside\n\n" + quote(end()),
])
def test_pair_cannot_cross_ast_containers(source):
    assert_invalid(source, "ANCHOR_CONTAINER_MISMATCH")


def test_outer_scope_can_cover_nested_container_content():
    result = parse(start() + quote(start("B") + "Inner\n\n" + end("B")) + "\n" + end())
    outer, inner = result.anchor_scopes
    assert outer.node_ids == inner.node_ids == (result.content_nodes[0].node_id,)
    assert outer.container_id != inner.container_id


def test_sibling_list_items_cannot_pair():
    first = "- " + start().replace("\n", "\n  ").rstrip() + "\n"
    second = "- " + end().replace("\n", "\n  ").rstrip() + "\n"
    assert_invalid(first + second, "ANCHOR_CONTAINER_MISMATCH")


def test_anchor_ids_are_source_local_and_not_global_state():
    source = start() + "First source\n" + end()
    other = start() + "Second source\n" + end()
    assert parse(source).anchor_scopes[0].kg_ref == parse(other).anchor_scopes[0].kg_ref


def test_clean_source_preserves_crlf_inside_anchors():
    source = (start() + "Si 6.50 wt.%\n" + end()).replace("\n", "\r\n")
    result = parse(source.encode("utf-8-sig"))
    assert result.clean_source == "Si 6.50 wt.%\r\n"
    assert result.content_nodes[0].source == result.clean_source


def test_empty_scope_does_not_expand_or_make_fake_content():
    result = parse("Before\n\n" + start() + end() + "\nAfter\n")
    assert result.anchor_scopes[0].node_ids == ()
    assert [node.source.strip() for node in result.content_nodes] == ["Before", "After"]
    only_pair = parse(start() + end())
    assert only_pair.content_nodes == ()
    assert only_pair.clean_source == ""


@pytest.mark.parametrize("wrap", [lambda value: value, quote])
def test_controls_are_absent_from_all_clean_ast_fields_and_extracted_source(wrap):
    result = parse(wrap(start(anchor_type="table", table_line="table_ref: T1\n") + "| Si | 6.50 |\n| --- | --- |\n" + end()))
    visible = json.dumps(asdict(result.root), ensure_ascii=False) + result.clean_source
    visible += "".join(node.source + node.content for node in result.content_nodes)
    for control in ("kg-anchor-start", "kg-anchor-end", "anchor_id", "graph_id", "anchor_type", "table_ref"):
        assert control not in visible
    assert "6.50" in visible


def test_invalid_metadata_error_does_not_expose_source_or_exception_context():
    secret = "private-table-value-712.389"
    error = assert_invalid(start(table_line='table_ref: "' + secret + '\n') + end(), "ANCHOR_METADATA_INVALID")
    assert secret not in json.dumps(error.to_api_error().model_dump())
    assert secret not in str(error)


def test_scope_coverage_uses_ast_identity_not_line_ranges():
    from app.ingestion.markdown.anchors import parse_anchors
    from app.ingestion.markdown.parser import parse_markdown_ast

    ast = parse_markdown_ast("Outside\n\n" + start() + "Inside\n" + end())
    expected = parse_anchors(ast)
    inside = next(node for node in ast.root.children if node.kind == "paragraph" and "Inside" in node.source)
    # Provenance deliberately points to another paragraph; it must not change coverage.
    outside = ast.root.children[0]
    changed = replace(ast, root=replace(ast.root, children=tuple(
        replace(node, source_range=outside.source_range) if node.node_id == inside.node_id else node
        for node in ast.root.children
    )))
    actual = parse_anchors(changed)
    assert actual.anchor_scopes[0].node_ids == expected.anchor_scopes[0].node_ids == (inside.node_id,)
