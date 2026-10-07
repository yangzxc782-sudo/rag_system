"""M1 pure renderer tests; no PDF parser, database, model or object store."""
from dataclasses import replace
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.ingestion.frozen_source import (
    json_bytes, render_frozen_source, strip_kg_markers, verify_frozen_source,
)


def block(index, value, *, kind="text", page=0, **extra):
    return SimpleNamespace(id=uuid4(), block_index=index, block_key=f"b{index}", block_type=kind,
                           page_start=page, page_end=page, text=value, **extra)


def render(blocks):
    identity = dict(document_id=uuid4(), parse_run_id=uuid4(), source_version=uuid4())
    for item in blocks:
        item.document_id = identity["document_id"]
        item.parse_run_id = identity["parse_run_id"]
    return render_frozen_source(blocks, **identity, output_prefix="run"), identity


def verify(frozen, identity):
    return verify_frozen_source(frozen.canonical, frozen.block_map, **identity,
                                canonical_sha256=frozen.canonical_sha256,
                                block_map_sha256=frozen.block_map_sha256,
                                character_count=frozen.character_count)


def test_offsets_are_frozen_unicode_characters_with_duplicate_text_and_titles():
    items = [block(0, "同标题", kind="title"), block(1, "e\u0301😀\r\n重复"),
             block(2, "同标题", kind="title", page=1), block(3, "é😀\n重复", page=1)]
    frozen, identity = render(items)
    text = verify(frozen, identity)
    assert text == "同标题\n\né😀\n重复\n\n同标题\n\né😀\n重复"
    directory = json.loads(frozen.block_map)
    assert frozen.character_count == len(text) != len(frozen.canonical)
    assert directory["blocks"][1]["source_start"] != directory["blocks"][3]["source_start"]
    for item, entry, expected in zip(items, directory["blocks"], ["同标题", "é😀\n重复", "同标题", "é😀\n重复"]):
        assert text[entry["source_start"]:entry["source_end"]] == expected
        assert entry["block_id"] == str(item.id) and entry["page_start"] == item.page_start
        assert not any(key.endswith("spans") for key in entry)


@pytest.mark.parametrize("prefix,suffix", [("", ""), ("正常前文", "正常后文"), ("😀\n", "\né")])
def test_complete_multiline_control_comments_removed_before_coordinates(prefix, suffix):
    value = prefix + '<!-- kg-anchor-start\nanchor_id: CONTROL\ngraph_id: SECRET\n-->正文<!-- kg-anchor-end\nanchor_id: CONTROL\n-->' + suffix
    frozen, identity = render([block(0, value)])
    assert verify(frozen, identity) == prefix + "正文" + suffix
    assert b"CONTROL" not in frozen.canonical and b"SECRET" not in frozen.canonical
    assert b"kg-anchor" not in frozen.canonical


def test_ordinary_comment_and_formula_table_image_indentation_are_preserved():
    table = '<table><tr><td>≥350 MPa</td><td>0.2%</td></tr></table>'
    items = [block(0, "", kind="table", html=table, caption="表1"),
             block(1, "", kind="formula", latex=r"A \wedge B = 3"),
             block(2, "图注", kind="image", source_metadata={"asset_keys": ["run/images/a.png"]}),
             block(3, "    x = 3\n    y = 4", source_metadata={"original_type": "code"}),
             block(4, "<!-- 普通注释 -->正文")]
    frozen, identity = render(items)
    text = verify(frozen, identity)
    assert table in text and r"A \wedge B = 3" in text
    assert "![](images/a.png)" in text and "    x = 3\n    y = 4" in text
    assert "<!-- 普通注释 -->正文" in text


@pytest.mark.parametrize("content", ["<!-- kg-anchor-start\nanchor_id: X", "<!--kg-anchor-end X"])
def test_unterminated_controls_block_freezing(content):
    with pytest.raises(ValueError, match="Unterminated"):
        render([block(0, content)])


def test_invalid_empty_or_reordered_source_is_rejected():
    with pytest.raises(ValueError, match="no canonical"):
        render([block(0, "页眉", kind="header")])
    with pytest.raises(ValueError, match="stable index"):
        render([block(3, "后"), block(2, "前")])
    assert strip_kg_markers("前<!-- kg-anchor-start X -->后") == "前后"


def test_owner_mismatch_rejected_and_initial_bom_removed_before_freezing():
    items = [block(0, "\ufeff正文")]
    frozen, identity = render(items)
    assert verify(frozen, identity) == "正文"
    with pytest.raises(ValueError, match="ownership"):
        render_frozen_source(items, **{**identity, "document_id": uuid4()}, output_prefix="run")


def test_reader_rejects_mutated_bytes_identity_and_directory_gaps():
    frozen, identity = render([block(0, "甲"), block(1, "乙")])
    with pytest.raises(ValueError, match="hash"):
        verify(replace(frozen, canonical=frozen.canonical + b"x"), identity)
    with pytest.raises(ValueError, match="identity"):
        verify(frozen, {**identity, "source_version": uuid4()})
    directory = json.loads(frozen.block_map)
    directory["blocks"][1]["source_start"] += 1
    raw = json_bytes(directory)
    with pytest.raises(ValueError, match="range"):
        verify(replace(frozen, block_map=raw, block_map_sha256=sha256(raw).hexdigest()), identity)
