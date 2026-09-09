from __future__ import annotations

import ast
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
from uuid import UUID

import pytest

from app.ingestion.block_chunker import BlockChunkerConfig
from app.ingestion.markdown.parser import parse_markdown
from test_kg_anchor_parser import end, start


def config(size=1000, overlap=0, **kwargs):
    return BlockChunkerConfig(
        max_chunk_chars=size, min_chunk_chars=0, overlap_chars=overlap, **kwargs
    )


def build(document, settings=None):
    from app.ingestion.markdown.chunker import build_markdown_chunks

    return build_markdown_chunks(document, config=settings or config())


def chunks(source, size=1000, overlap=0):
    return build(parse_markdown(source), config(size, overlap))


def ids(draft):
    return draft.source_metadata["source_range"]["ast_node_ids"]


def refs(draft):
    return [ref["anchor_id"] for ref in draft.source_metadata["kg_refs"]]


def test_draft_has_neutral_persistence_fields_without_orm():
    from app.ingestion.chunk_drafts import ChunkBlockRef, ChunkDraft

    draft, = chunks("Body\n")
    assert isinstance(draft, ChunkDraft)
    assert draft.chunk_index == 0
    assert draft.parse_run_id is None
    assert draft.block_refs == ()
    assert draft.page_start is draft.page_end is draft.section_title is None
    assert draft.chunk_method == "markdown_ast"
    assert draft.content_format == "markdown"
    assert not hasattr(draft, "embedding")
    assert not hasattr(draft, "id")
    block_id = UUID("00000000-0000-0000-0000-000000000001")
    run_id = UUID("00000000-0000-0000-0000-000000000002")
    neutral = replace(draft, parse_run_id=run_id, block_refs=(ChunkBlockRef(block_id, 0),))
    assert neutral.block_refs[0].block_id == block_id
    assert neutral.parse_run_id == run_id


def test_plain_heading_and_paragraph_include_heading_content_once():
    source = "# Casting\n\nPrepare the mould.\n"
    draft, = chunks(source)
    assert draft.content == source
    assert draft.section_title == "Casting"
    assert draft.source_metadata["section_path"] == ["Casting"]
    assert draft.chunk_type == "text"


def test_heading_hierarchy_and_repeated_heading_form_boundaries():
    source = "# 第8章\n\nA\n\n## **化学成分**\n\nB\n\n### 铝合金\n\nC\n\n## 参数\n\nD\n\n# 第8章\n\nE\n"
    result = chunks(source, overlap=100)
    assert [draft.source_metadata["section_path"] for draft in result] == [
        ["第8章"], ["第8章", "化学成分"], ["第8章", "化学成分", "铝合金"],
        ["第8章", "参数"], ["第8章"],
    ]
    assert "D" not in result[-1].content
    assert result[-1].content.startswith("# 第8章")


def test_skipped_heading_levels_do_not_invent_sections():
    result = chunks("# A\n\nFirst\n\n### C\n\nSecond\n\n## B\n\nThird\n")
    assert [draft.source_metadata["section_path"] for draft in result] == [["A"], ["A", "C"], ["A", "B"]]


TABLE = "| 成分 | 下限 | 上限 |\n| --- | ---: | ---: |\n| Si | ≥6.50% | ≤7.50% |\n"


@pytest.mark.parametrize("size", [1000, 20])
def test_table_is_atomic_even_when_oversized(size):
    document = parse_markdown("Before\n\n" + TABLE + "\nAfter\n")
    result = build(document, config(size))
    table_draft, = [draft for draft in result if draft.chunk_type == "table"]
    assert table_draft.content == TABLE
    assert ids(table_draft) == [document.content_nodes[1].node_id]
    assert len(result) == 3


def test_mineru_table_flags_cannot_enable_markdown_table_splitting():
    draft, = build(parse_markdown(TABLE), config(20, keep_table_intact=False, max_table_chars=10))
    assert draft.content == TABLE


@pytest.mark.parametrize("source,kind", [
    ("```python\n" + "value = 0.050\n" * 8 + "```\n", "code"),
    ("    " + "value = 0.050 " * 12 + "\n", "code"),
    ("$$\nQ = mc\\Delta T + " + "x + " * 30 + "y\n$$\n", "formula"),
    ("\\[\n" + "x + " * 30 + "y\n\\]\n", "formula"),
    ("$$a = " + "x + " * 30 + "y$$\n", "formula"),
    ("<div>" + "raw 0.050 mm " * 20 + "</div>\n", "raw"),
])
def test_code_formula_and_raw_blocks_remain_atomic(source, kind):
    draft, = chunks(source, size=30)
    assert draft.content == source
    assert draft.chunk_type == kind


def test_list_keeps_nested_structure_and_real_descendant_ids():
    source = "- Prepare\n  - Dry\n  - Inspect\n- Pour\n"
    document = parse_markdown(source)
    draft, = build(document, config(12))
    assert draft.content == source
    assert draft.chunk_type == "list"
    assert ids(draft) == [node.node_id for node in document.content_nodes]
    assert document.root.node_id not in ids(draft)
    assert document.root.children[0].node_id not in ids(draft)


def test_quoted_heading_does_not_change_document_section_hierarchy():
    result = chunks("# Actual section\n\n> # Quoted title\n>\n> 6.50%\n\nAfter\n")
    quote_draft, = [draft for draft in result if draft.chunk_type == "blockquote"]
    assert "> # Quoted title" in quote_draft.content
    assert all(draft.source_metadata["section_path"] == ["Actual section"] for draft in result)


def test_compatible_paragraphs_combine():
    source = "First paragraph.\n\nSecond paragraph.\n"
    draft, = chunks(source)
    assert draft.content == source
    assert len(ids(draft)) == 2


def test_long_plain_paragraph_splits_losslessly_and_retains_node_identity():
    source = "one two three four five six seven eight nine ten " * 8 + "end.\n"
    document = parse_markdown(start() + source + end())
    result = build(document, config(45))
    assert len(result) >= 3
    assert "".join(draft.content for draft in result) == source
    assert all(len(draft.content) <= 45 for draft in result)
    assert all(ids(draft) == [document.content_nodes[0].node_id] for draft in result)
    assert all(refs(draft) == ["A"] for draft in result)


def test_chinese_sentence_boundaries_and_unbroken_numeric_atom():
    sentence = "温度≤720℃，厚度≥3.50mm。"
    result = chunks(sentence * 10 + "\n", size=25)
    assert len(result) > 1
    assert "".join(draft.content for draft in result) == sentence * 10 + "\n"
    assert all(sentence in draft.content for draft in result)
    atom = "≥123456789012345678901234567890mm\n"
    assert chunks(atom, size=8)[0].content == atom


def test_inline_markdown_is_not_split_through_code_or_links():
    source = "Use `a + b + c` and [the specification](https://example.test/standard) safely.\n"
    draft, = chunks(source, size=15)
    assert draft.content == source


def test_whole_node_overlap_and_overlap_anchor_inheritance():
    source = start("A") + "AAAAAAAAAA\n" + end("A") + "\n" + start("B") + "BBBBBBBBBB\n" + end("B") + "\nCCCCCCCCCC\n"
    document = parse_markdown(source)
    result = build(document, config(30, 12))
    assert len(result) == 3
    assert result[1].content == "AAAAAAAAAA\n\nBBBBBBBBBB\n"
    assert ids(result[1]) == [node.node_id for node in document.content_nodes[:2]]
    assert refs(result[1]) == ["A", "B"]
    assert refs(result[2]) == ["B"]  # overlap is from base content, never recursive.


def test_overlap_never_slices_atomic_nodes():
    result = chunks(TABLE + "\nAfter table.\n", size=100, overlap=10)
    assert len(result) == 2
    assert result[0].content == TABLE
    assert result[1].content == "After table.\n"


def test_complete_atomic_node_can_overlap_when_both_budgets_allow():
    code = "```\nx\n```\n"
    result = chunks(code + "\nAfter\n", size=40, overlap=15)
    assert result[1].content == code + "\nAfter\n"
    assert len(ids(result[1])) == 2


def test_fragment_overlap_keeps_origin_and_does_not_split_words():
    source = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda " * 6 + "omega\n"
    document = parse_markdown(start() + source + end())
    result = build(document, config(55, 18))
    assert len(result) > 2
    assert all(len(draft.content) <= 55 for draft in result)
    assert all(ids(draft) == [document.content_nodes[0].node_id] and refs(draft) == ["A"] for draft in result)
    assert set(result[0].content.split()) & set(result[1].content.split())
    assert all(word in source.split() for draft in result for word in draft.content.split())


def test_one_anchor_covers_many_node_based_chunks():
    document = parse_markdown(start() + "\n\n".join(["First 6.50%", "Second ≥3mm", "Third 720℃"]) + "\n" + end())
    result = build(document, config(15))
    assert len(result) == 3
    assert all(refs(draft) == ["A"] for draft in result)
    assert {node for draft in result for node in ids(draft)} == set(document.anchor_scopes[0].node_ids)


def test_many_anchors_combine_in_source_order_not_lexical_order():
    source = start("Z") + "First\n" + end("Z") + "\n" + start("A") + "Second\n" + end("A")
    draft, = chunks(source)
    assert refs(draft) == ["Z", "A"]


def test_nested_anchor_inheritance_only_applies_to_used_nodes():
    source = start("A") + "Outer one\n\n" + start("B") + "Inner two\n" + end("B") + "\nOuter three\n" + end("A")
    result = chunks(source, size=13)
    assert [refs(draft) for draft in result] == [["A"], ["A", "B"], ["A"]]


def test_duplicate_scopes_are_deduplicated_and_ties_use_anchor_id():
    document = parse_markdown(start("Z") + start("A") + "Both\n" + end("A") + end("Z"))
    z, a = document.anchor_scopes
    document = replace(document, anchor_scopes=(replace(z, start_order=a.start_order), a, a))
    draft, = build(document)
    assert refs(draft) == ["A", "Z"]


def test_empty_scope_does_not_attach_to_adjacent_content_or_emit_drafts():
    assert chunks(start() + end()) == []
    draft, = chunks("Before\n\n" + start() + end() + "\nAfter\n")
    assert refs(draft) == []


@pytest.mark.parametrize("source", ["", "\n\n", "<!-- ordinary -->\n"])
def test_empty_and_unanchored_markdown_have_array_contract(source):
    result = chunks(source)
    assert all(draft.source_metadata["kg_refs"] == [] for draft in result)
    if not source.strip():
        assert result == []


def test_null_table_ref_and_complete_metadata_shape():
    document = parse_markdown(start(table_line="table_ref: null\n") + "Body\n" + end())
    draft, = build(document)
    node = document.content_nodes[0]
    assert draft.source_metadata == {
        "parser_provider": "markdown_native", "parser_version": document.parser_version,
        "chunk_method": "markdown_ast", "content_format": "markdown", "section_path": [],
        "source_range": {"kind": "markdown_ast", "start_line": node.source_range.start_line,
                         "end_line": node.source_range.end_line, "ast_node_ids": [node.node_id]},
        "kg_refs": [{"anchor_id": "A", "graph_id": "G::2026", "anchor_type": "section", "table_ref": None}],
    }
    assert '"table_ref": null' in json.dumps(asdict(draft))
    assert "kg_ref" not in draft.source_metadata


def test_noncontiguous_source_nodes_record_actual_ids_and_original_line_envelope():
    document = parse_markdown(start() + "First\n" + end() + "\n" + start("B") + "Second\n" + end("B"))
    first, second = document.content_nodes
    draft, = build(document)
    assert draft.source_metadata["source_range"] == {
        "kind": "markdown_ast", "start_line": first.source_range.start_line,
        "end_line": second.source_range.end_line,
        "ast_node_ids": [first.node_id, second.node_id],
    }


def test_inheritance_uses_ids_not_lines_text_or_section_name():
    document = parse_markdown(start("A") + "Identical\n" + end("A") + "\nIdentical\n")
    first, second = document.root.children
    second = replace(second, source_range=first.source_range)
    document = replace(document, root=replace(document.root, children=(first, second)))
    result = build(document, config(10))
    assert [refs(draft) for draft in result] == [["A"], []]


def test_heading_metadata_does_not_reintroduce_its_anchor_on_later_chunks():
    source = start("TITLE") + "# Section\n" + end("TITLE") + "\nFirst text\n\nSecond text\n"
    result = chunks(source, size=12)
    assert refs(result[0]) == ["TITLE"]
    assert all(refs(draft) == [] and draft.section_title == "Section" for draft in result[1:])


def test_controls_never_appear_in_final_draft_content():
    source = start("A", anchor_type="table", table_line="table_ref: T1\n") + TABLE + end("A")
    for draft in chunks(source, size=20, overlap=5):
        for marker in ("kg-anchor-start", "kg-anchor-end", "anchor_id:", "graph_id:", "anchor_type:", "table_ref:"):
            assert marker not in draft.content


def test_content_fidelity_and_line_endings():
    table = TABLE.replace("\n", "\r\n")
    draft, = chunks(table.encode("utf-8-sig"), size=8)
    assert draft.content == table
    expression = "厚度 ≥3.50mm；温度 ≤720℃；成分 6.50–7.50%；压力 0.050MPa。\n"
    result = chunks(expression, size=15)
    assert "".join(draft.content for draft in result) == expression


def test_token_count_follows_existing_estimate_and_size_config():
    result = chunks("one two three four five six seven eight nine\n", size=20)
    assert len(result) > 1
    assert all(draft.token_count == max(1, math.ceil(len(draft.content) / 4)) for draft in result)
    assert all(draft.token_count <= math.ceil(20 / 4) for draft in result)


def test_two_config_rechunk_is_deterministic_without_mutating_source_or_anchors():
    source = "# Standard\n\n" + start("A") + "First parameter.\n\n" + start("B") + TABLE + end("B") + "\n" + "some long paragraph words " * 20 + "\n" + end("A")
    document = parse_markdown(source)
    before = asdict(document)
    small = build(document, config(45, 10))
    large = build(document, config(1000))
    assert len(small) != len(large)
    assert [asdict(draft) for draft in small] == [asdict(draft) for draft in build(document, config(45, 10))]
    assert json.dumps([asdict(draft) for draft in small], sort_keys=True) == json.dumps([asdict(draft) for draft in build(document, config(45, 10))], sort_keys=True)
    assert asdict(document) == before
    for result in (small, large):
        assert [draft.chunk_index for draft in result] == list(range(len(result)))
        assert {node_id for draft in result for node_id in ids(draft)} == {node.node_id for node in document.content_nodes}
        for draft in result:
            expected = [scope.kg_ref.anchor_id for scope in document.anchor_scopes if set(ids(draft)) & set(scope.node_ids)]
            assert refs(draft) == expected


def test_draft_metadata_is_not_shared_across_results():
    document = parse_markdown(start() + "First\n\nSecond\n" + end())
    result = build(document, config(8))
    result[0].source_metadata["kg_refs"][0]["graph_id"] = "changed"
    assert result[1].source_metadata["kg_refs"][0]["graph_id"] == "G::2026"
    assert document.anchor_scopes[0].kg_ref.graph_id == "G::2026"


def test_m2_modules_have_no_orm_persistence_or_external_service_dependencies():
    app = Path(__file__).resolve().parents[1] / "app"
    for relative in ("ingestion/chunk_drafts.py", "ingestion/markdown/chunker.py"):
        tree = ast.parse((app / relative).read_text(encoding="utf-8"))
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        assert not any(name and name.startswith(("sqlalchemy", "neo4j", "app.models", "app.services", "app.rag")) for name in imports)
