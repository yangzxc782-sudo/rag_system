"""Pure M2 protocol/source tests. No providers, database or external writes."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.extraction.kg_protocol import ANCHOR_ADAPTER, AnchorEnd, format_anchor_end, format_anchor_start, validate_anchors
from app.extraction.kg_units import make_graph_id, render_tagged_source, split_units
from app.extraction.kg_extract import TEMPLATE_SHA256, qualify, strict_json, template
from app.ingestion.frozen_source import strip_kg_markers

GRAPH = "KG-20260929-GBT94382013"


def test_actual_package_annotation_samples():
    sample = json.loads(Path(__file__).with_name("fixtures").joinpath("kg_v2_package_anchors.json").read_text(encoding="utf-8"))
    assert sample["source_sha256"] == "6b2432c8dd52f8f2c6ebab06a74932fb6047d3543a67b0c5eb559db002eef0b4"
    refs = validate_anchors(sample["anchors"])
    assert {ref.anchor_type for ref in refs} == {"table", "clause"}
    assert all(ref.business_metadata() == raw for ref, raw in zip(refs, sample["anchors"]))


def test_plain_title_blocks_use_frozen_positions_not_find():
    text = "6 要求\n\n重复\n\n6.1 要求\n\n重复"
    blocks = [dict(block_type="title", source_start=0, source_end=4),
              dict(block_type="title", source_start=10, source_end=16)]
    units = split_units(text, GRAPH, block_directory=blocks)
    assert len(units) == 2
    assert [u.anchor.heading_ref for u in units] == ["6", "6.1"]
    assert all(text[u.source_start:u.source_end] == "重复" for u in units)


def anchor(kind="table"):
    local = ("T" if kind == "table" else "C") + "-6.1-1"
    return dict(graph_id=GRAPH, anchor_id=f"{GRAPH}::{local}", anchor_type=kind, **{f"{kind}_ref": local})


@pytest.mark.parametrize("kind", ["table", "clause"])
def test_anchor_fields_match_package_start_end(kind):
    data = anchor(kind)
    data.update(heading_ref="6.1", heading="6 技术要求 / 6.1 化学成分")
    if kind == "table":
        data["table_no"] = "2"
    ref = ANCHOR_ADAPTER.validate_python(data)
    start, end = format_anchor_start(ref), format_anchor_end(ref)
    assert f"{kind}_ref:" in start
    assert f"{'clause' if kind == 'table' else 'table'}_ref:" not in start
    assert end == f"<!-- kg-anchor-end\nanchor_id: {ref.anchor_id}\n-->"
    assert AnchorEnd(anchor_id=ref.anchor_id).model_dump() == {"anchor_id": ref.anchor_id}


@pytest.mark.parametrize("field,value", [
    ("anchor_type", "figure"), ("anchor_id", ""), ("graph_id", None), ("table_ref", ""),
    ("table_ref", "C-6.1-1"), ("table_ref", "T-9-1"), ("anchor_id", "OTHER::T-6.1-1"),
    ("table_no", 2), ("heading", ""), ("heading_ref", "a\nanchor_id: X"),
    ("heading", "x-->y"), ("graph_id", "x" * 129), ("local_ref", "T-6.1-1"), ("source_start", 1),
])
def test_bad_anchor_fields_rejected(field, value):
    with pytest.raises(ValidationError):
        ANCHOR_ADAPTER.validate_python({**anchor(), field: value})


@pytest.mark.parametrize("field", ["anchor_id", "graph_id", "anchor_type", "clause_ref"])
def test_clause_required_fields(field):
    data = anchor("clause")
    del data[field]
    with pytest.raises(ValidationError):
        ANCHOR_ADAPTER.validate_python(data)


def test_clause_does_not_accept_fake_table_and_duplicates_fail_closed():
    with pytest.raises(ValidationError):
        ANCHOR_ADAPTER.validate_python({**anchor("clause"), "table_ref": ""})
    assert len(validate_anchors([anchor(), anchor()])) == 1
    with pytest.raises(ValueError, match="Conflicting"):
        validate_anchors([anchor(), {**anchor(), "heading": "conflict"}])


def test_unicode_duplicate_titles_multiple_anchors_in_one_block_exact_original_offsets():
    text = "# 6 技术要求\n\n😀é重复\n\n表2\n<table><tr><td>$a^2$ ≥350 MPa</td></tr></table>尾文\n# 6 技术要求\n😀é重复"
    units = split_units(text, GRAPH)
    assert [u.anchor.anchor_type for u in units] == ["clause", "table", "clause", "clause"]
    assert [text[u.source_start:u.source_end] for u in units] == [
        "😀é重复", "表2\n<table><tr><td>$a^2$ ≥350 MPa</td></tr></table>", "尾文", "😀é重复"]
    assert units[0].source_start != units[-1].source_start
    assert units[0].anchor.anchor_id != units[-1].anchor.anchor_id
    assert units[1].anchor.table_no == "2"
    assert "$a^2$" in units[1].pieces[0]
    marked = render_tagged_source(text, units, {u.anchor.anchor_id for u in units})
    assert strip_kg_markers(marked).encode("utf-8") == text.encode("utf-8")


def test_adjacent_tables_and_heading_like_content_do_not_lose_text():
    text = '<table><tr><td>甲</td></tr></table><table><tr><td>乙</td></tr></table>\n后文'
    units = split_units(text, GRAPH)
    assert len(units) == 3 and units[0].source_end == units[1].source_start
    assert text[units[-1].source_start:units[-1].source_end] == "后文"


def test_long_table_pieces_keep_one_original_unit_and_anchor():
    text = "# 4 性能\n表3\n|牌号|性能|\n|---|---|\n" + "\n".join(f"|ZL{i}|≥350 MPa|" for i in range(85))
    unit, = split_units(text, GRAPH)
    assert len(unit.pieces) == 3
    assert unit.source_end == len(text)
    assert all("|牌号|性能|" in piece for piece in unit.pieces)
    assert unit.anchor.table_ref == "T-4-1"
    # Repeated headers are extraction input only, never a retrieval chunk.
    assert text[unit.source_start:unit.source_end].count("|牌号|性能|") == 1


def test_long_html_table_preserves_formula_colspan_and_does_not_cut_rowspan():
    rows = ['<tr><th>牌号</th><th>性能</th></tr>']
    for i in range(85):
        span = ' rowspan="3"' if i == 39 else ''
        rows.append(f'<tr><td{span}>ZL{i}</td><td colspan="2">$a^2$ ≥350 MPa</td></tr>')
    text = '表3\n<table>' + ''.join(rows) + '</table>'
    unit, = split_units(text, GRAPH)
    assert unit.source_start == 0 and unit.source_end == len(text) and len(unit.pieces) == 3
    assert all(piece.count('<table>') == piece.count('</table>') == 1 for piece in unit.pieces)
    assert 'ZL39' in unit.pieces[0] and 'ZL41' in unit.pieces[0] and 'ZL42' in unit.pieces[1]
    assert all('$a^2$ ≥350 MPa' in piece and 'colspan="2"' in piece for piece in unit.pieces)
    assert strip_kg_markers(render_tagged_source(text, (unit,), {unit.anchor.anchor_id})) == text


@pytest.mark.parametrize("text", ["<table>missing end", "<table><table></table></table>", "<!-- kg-anchor-start\nanchor_id: bad\n-->正文"])
def test_malformed_or_tagged_input_rejected(text):
    with pytest.raises(ValueError):
        split_units(text, GRAPH)


def test_graph_id_matches_reference_and_collision_is_visible():
    day = date(2026, 9, 29)
    assert make_graph_id("GB-T 9438_2013.pdf", day) == GRAPH
    assert make_graph_id("中文甲.pdf", day) == make_graph_id("中文乙.pdf", day) == "KG-20260929-DOC"


def raw_part():
    return dict(entities=[dict(id="e1", type="牌号", name="ZL101", properties={}, provenance={"原文": "ZL101"}),
                          dict(id="e2", type="力学性能", name="拉伸性能", properties={"抗拉强度数值": {
                              "raw": "≥350", "value": 350, "unit": "MPa", "comparator": ">="}}, provenance={"原文": "≥350 MPa"})],
                relationships=[dict(source_id="e1", target_id="e2", type="具有力学性能", properties={}, provenance={"原文": "ZL101 ≥350 MPa"})])


@pytest.mark.parametrize("kind", ["table", "clause"])
def test_qualification_binds_template_entities_and_relations(kind):
    result = qualify(raw_part(), anchor(kind), 2)
    assert len(result["relationships"]) == 1
    assert all(e["id"].startswith(anchor(kind)["anchor_id"] + "::p2::") for e in result["entities"])
    assert result["entities"][1]["properties"]["抗拉强度数值"]["unit"] == "MPa"
    assert "table_ref" in result["entities"][0]["properties"] if kind == "table" else "table_ref" not in result["entities"][0]["properties"]


def test_actual_current_template_is_v4_and_not_old_ontology_template():
    pack = template()
    assert pack["version"] == "4.0.0" and len(TEMPLATE_SHA256) == 64
    assert "铸件" in pack["entity_type_whitelist"]
    assert "具有牌号" in {r["type"] for r in pack["relation_type_whitelist"]}
    for sample in pack["examples"]:
        result = qualify(sample["example_output"], anchor("clause"), 0)
        assert result["relationships"]


@pytest.mark.parametrize("change", ["type", "endpoint", "from_to"])
def test_valid_output_without_qualified_triples_is_diagnostic_empty(change):
    data = raw_part()
    if change == "type":
        data["relationships"][0]["type"] = "INVENTED"
    elif change == "endpoint":
        data["relationships"][0]["target_id"] = "missing"
    else:
        data["relationships"][0]["type"] = "具有"
    result = qualify(data, anchor(), 0)
    assert result["relationships"] == [] and result["rejected_relationships"] == 1


def test_sanitized_ids_duplicate_conflicts_and_model_controls_rejected():
    data = raw_part()
    extra = deepcopy(data["entities"][0])
    extra["id"] = "e-1"
    data["entities"].append(extra)
    with pytest.raises(ValueError, match="collision"):
        qualify(data, anchor(), 0)
    data = raw_part()
    data["entities"][0]["properties"]["anchor_id"] = "foreign"
    with pytest.raises(ValueError, match="control"):
        qualify(data, anchor(), 0)


@pytest.mark.parametrize("raw", ['{}', '{"entities":[],"relationships":null}', '{"entities":[]}'])
def test_structurally_invalid_results_are_errors_not_empty_success(raw):
    with pytest.raises(ValidationError):
        qualify(json.loads(raw), anchor(), 0)


@pytest.mark.parametrize("raw", ['{"entities":[],"entities":[]}', '{"n":NaN}', '[]', '{"a":"\\ud800"}'])
def test_strict_response_json(raw):
    with pytest.raises((ValueError, UnicodeError)):
        strict_json(raw)
