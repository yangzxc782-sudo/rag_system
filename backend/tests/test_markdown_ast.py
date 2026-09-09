from __future__ import annotations

from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
import tomllib

import pytest

from app.core.errors import BusinessError


def parse(source: str | bytes):
    from app.ingestion.markdown.parser import parse_markdown

    return parse_markdown(source)


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def test_markdown_it_dependency_is_explicit_and_pinned():
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert "markdown-it-py==4.2.0" in project["project"]["dependencies"]
    assert version("markdown-it-py") == "4.2.0"


def test_plain_markdown_without_anchors():
    source = "# 铸造工艺\n\n温度 **720 ± 5 °C**；质量 0.050 kg。\n"
    result = parse(source)
    assert result.anchor_scopes == ()
    assert result.clean_source == source
    assert [node.kind for node in result.content_nodes] == ["heading", "paragraph"]
    assert result.parser_version == "markdown-it-py/4.2.0"


@pytest.mark.parametrize("source", ["", "\n\n", b"", b"\xef\xbb\xbf"])
def test_empty_source_has_no_content_nodes(source):
    result = parse(source)
    assert result.content_nodes == ()
    assert result.anchor_scopes == ()


def test_markdown_table_numbers_and_inline_structure_are_preserved():
    table = "| 成分 | 下限 wt.% | 上限 wt.% |\n| :--- | ---: | ---: |\n| Si | 6.50 | ≤7.50 |\n"
    result = parse(table)
    assert len(result.content_nodes) == 1
    node = result.content_nodes[0]
    assert node.kind == "table"
    assert node.source == table
    assert {child.kind for child in walk(node)} >= {"thead", "tbody", "tr", "th", "td", "inline", "text"}
    assert result.clean_source == table


def test_atx_and_setext_headings_preserve_levels_and_source():
    source = "# 总则\n\n## 参数\n\n成分\n====\n"
    nodes = parse(source).content_nodes
    assert [node.tag for node in nodes] == ["h1", "h2", "h1"]
    assert nodes[-1].source == "成分\n====\n"


def test_lists_code_formula_and_raw_html_are_retained_without_html_roundtrip(monkeypatch):
    from markdown_it import MarkdownIt

    def forbidden_render(*args, **kwargs):
        raise AssertionError("AST ingestion must not render HTML")

    monkeypatch.setattr(MarkdownIt, "render", forbidden_render)
    source = (
        "- First **item**\n- Second `item`\n\n"
        "1. Pour\n2. Cool\n\n"
        "```python\nvalue = 0.050\n```\n\n"
        "    untouched = 1e-3\n\n"
        "$$\nQ = mc\\Delta T\n$$\n\n"
        "<div>1 &lt; 2</div>\n"
    )
    result = parse(source)
    assert result.clean_source == source
    assert {node.kind for node in walk(result.root)} >= {
        "bullet_list", "ordered_list", "list_item", "strong", "code_inline",
        "fence", "code_block", "paragraph", "html_block",
    }
    assert any(node.source == "$$\nQ = mc\\Delta T\n$$\n" for node in result.content_nodes)


def test_ids_and_preorder_are_deterministic_and_containers_are_not_content():
    source = "# A\n\n- Same\n- Same\n\n> Quoted\n"
    first, second = parse(source), parse(source)
    assert asdict(first) == asdict(second)
    all_nodes = list(walk(first.root))
    assert len({node.node_id for node in all_nodes}) == len(all_nodes)
    assert [node.order for node in all_nodes] == sorted(node.order for node in all_nodes)
    assert len(first.content_nodes) == 4
    assert all(node.kind not in {"root", "bullet_list", "list_item", "blockquote"} for node in first.content_nodes)


def test_source_lines_are_one_based_inclusive_and_preserve_crlf():
    result = parse("\r\n## 参数\r\n\r\n数值 0.050\r\n下一行\r\n")
    heading, paragraph = result.content_nodes
    assert (heading.source_range.start_line, heading.source_range.end_line) == (2, 2)
    assert (paragraph.source_range.start_line, paragraph.source_range.end_line) == (4, 5)
    assert paragraph.source == "数值 0.050\r\n下一行\r\n"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_utf8_and_bom_are_lossless(encoding):
    source = "# 合金\n\nSi ≥ 6.50 wt.%；720 ± 5 °C\n"
    assert parse(source.encode(encoding)) == parse(source)


@pytest.mark.parametrize("source", [b"\xff\xfe", b"# Value\n\xe4\xb8", b"0.05\x80", "\ud800"])
def test_invalid_encoding_uses_safe_business_error(source):
    with pytest.raises(BusinessError) as caught:
        parse(source)
    error = caught.value
    assert error.code == "DOCUMENT_MARKDOWN_INVALID"
    assert error.status_code == 400
    assert error.detail["reason"] == "MARKDOWN_ENCODING_INVALID"
    assert set(error.detail) <= {"reason", "line", "anchor_id"}
    assert error.__context__ is None


def test_legacy_kg_tag_is_ordinary_content_without_anchor_inference():
    source = '<!-- kg-tag {"entity_id":"E1","chunk_id":"old"} -->\n\nLegacy body\n'
    result = parse(source)
    assert result.anchor_scopes == ()
    assert result.clean_source == source

