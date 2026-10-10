"""M1 pure renderer tests; no PDF parser, database, model or object store."""
from dataclasses import replace
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.ingestion.frozen_source import (
    json_bytes, render_frozen_source, strip_kg_markers, verify_frozen_source,
)
from pdf_source_fixtures import structured_source_blocks


def block(index, value, *, kind="text", page=0, **extra):
    return SimpleNamespace(id=uuid4(), block_index=index, block_key=f"b{index}", block_type=kind,
                           page_start=page, page_end=page, text=value, **extra)


def render(blocks, *, registered_asset_keys=()):
    identity = dict(document_id=uuid4(), parse_run_id=uuid4(), source_version=uuid4())
    for item in blocks:
        item.document_id = identity["document_id"]
        item.parse_run_id = identity["parse_run_id"]
    return render_frozen_source(blocks, **identity, output_prefix="run",
                                registered_asset_keys=registered_asset_keys), identity


def verify(frozen, identity):
    return verify_frozen_source(frozen.canonical, frozen.block_map, **identity,
                                canonical_sha256=frozen.canonical_sha256,
                                block_map_sha256=frozen.block_map_sha256,
                                character_count=frozen.character_count, output_prefix="run")


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
    frozen, identity = render(items, registered_asset_keys=["run/images/a.png"])
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
        render_frozen_source(items, **{**identity, "document_id": uuid4()}, output_prefix="run",
                             registered_asset_keys=[])


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


def changed_directory(frozen, change):
    directory = json.loads(frozen.block_map)
    change(directory)
    raw = json_bytes(directory)
    return replace(frozen, block_map=raw, block_map_sha256=sha256(raw).hexdigest())


def test_v2_freezes_sections_formats_and_ordered_owned_assets_without_mutation():
    items = [
        block(0, "前言"),
        block(1, "6 要求", kind="title", section_path=["6 要求"]),
        block(2, "正文"),
        block(3, "页眉", kind="header", section_path=["不得继承"]),
        block(4, "", kind="table", html="<table><tr><td>合成</td></tr></table>"),
        block(5, "6.1 子节", kind="title",
              source_metadata={"section_path": ["6 要求", "6.1 子节"]}),
        block(6, "", kind="formula", latex="x = 1"),
        block(7, "图注", kind="image", source_metadata={
            "asset_keys": ["run/images/b.png", "run/images/a.png", "run/images/b.png"]}),
        block(8, "附录", kind="title"),
        block(9, "尾文"),
    ]
    frozen, identity = render(items, registered_asset_keys=["run/images/a.png", "run/images/b.png"])
    original = deepcopy([vars(item) for item in items])
    again = render_frozen_source(items, **identity, output_prefix="run",
                                 registered_asset_keys=["run/images/b.png", "run/images/a.png"])
    assert frozen == again
    assert [vars(item) for item in items] == original
    directory = json.loads(frozen.block_map)
    assert directory["schema_version"] == 2
    entries = directory["blocks"]
    assert [entry["block_index"] for entry in entries] == [0, 1, 2, 4, 5, 6, 7, 8, 9]
    assert [entry["section_path"] for entry in entries] == [
        [], ["6 要求"], ["6 要求"], ["6 要求"],
        ["6 要求", "6.1 子节"], ["6 要求", "6.1 子节"], ["6 要求", "6.1 子节"],
        ["附录"], ["附录"],
    ]
    assert [entry["content_format"] for entry in entries] == [
        "plain_text", "markdown", "plain_text", "markdown", "markdown", "markdown",
        "markdown", "markdown", "plain_text",
    ]
    assert entries[6]["asset_keys"] == ["run/images/b.png", "run/images/a.png"]
    assert all(entry["asset_keys"] == [] for i, entry in enumerate(entries) if i != 6)
    # Persisted source-map is sufficient; subsequent mutable block edits cannot supply fields.
    items[1].section_path.append("后续修改")
    items[7].source_metadata["asset_keys"].clear()
    assert verify(frozen, identity) == frozen.canonical.decode("utf-8")
    assert json.loads(frozen.block_map) == directory


def test_explicit_block_structure_precedes_metadata_and_is_copied():
    items = [block(0, "正文", section_path=["显式章"], asset_keys=["run/a.png"],
                   source_metadata={"section_path": ["备选章"], "asset_keys": ["run/b.png"]})]
    frozen, identity = render(items, registered_asset_keys=["run/a.png", "run/b.png"])
    entry = json.loads(frozen.block_map)["blocks"][0]
    assert entry["section_path"] == ["显式章"]
    assert entry["asset_keys"] == ["run/a.png"]
    assert verify(frozen, identity) == "正文"


def test_structural_enrichment_preserves_pre_v2_canonical_bytes_and_coordinates():
    blocks, identity = structured_source_blocks()
    frozen = render_frozen_source(blocks, **identity, output_prefix="run", registered_asset_keys=[])
    assert verify(frozen, identity) == frozen.canonical.decode("utf-8")
    # Captured from the unchanged renderer before source-map v2 was introduced.
    assert frozen.canonical_sha256 == "3257bef8d09ff609e3bbf688635e408278e2f7e7907264916907357f2db4f854"
    assert frozen.character_count == 3122
    assert [(b["source_start"], b["source_end"]) for b in json.loads(frozen.block_map)["blocks"]] == [
        (0, 4), (6, 11), (13, 3092), (3094, 3110), (3112, 3118), (3120, 3122),
    ]


@pytest.mark.parametrize("version", [1, 3, True, 2.0, "2", None])
def test_reader_rejects_old_unknown_or_coerced_schema_version(version):
    frozen, identity = render([block(0, "正文")])
    changed = changed_directory(frozen, lambda d: d.update(schema_version=version))
    with pytest.raises(ValueError, match="identity"):
        verify(changed, identity)


@pytest.mark.parametrize("field", ["section_path", "content_format", "asset_keys"])
def test_reader_rejects_missing_structure_even_with_valid_hash(field):
    frozen, identity = render([block(0, "正文")])
    changed = changed_directory(frozen, lambda d: d["blocks"][0].pop(field))
    with pytest.raises(ValueError, match="fields"):
        verify(changed, identity)


@pytest.mark.parametrize("field,value", [
    ("section_path", "章"), ("section_path", [1]), ("section_path", [""]),
    ("section_path", None), ("content_format", "html"), ("content_format", None),
    ("asset_keys", "run/a.png"), ("asset_keys", [None]),
    ("asset_keys", ["run/a.png", "run/a.png"]),
    ("block_type", "invented"), ("block_type", "footer"), ("block_key", 1),
    ("block_id", None), ("page_start", True), ("page_start", -1), ("page_end", -1),
    ("block_index", True), ("source_end", 2.0), ("unexpected", []),
])
def test_reader_rejects_malformed_structural_fields_without_coercion(field, value):
    frozen, identity = render([block(0, "正文")])
    changed = changed_directory(frozen, lambda d: d["blocks"][0].update({field: value}))
    with pytest.raises(ValueError):
        verify(changed, identity)


@pytest.mark.parametrize("key", [
    "other/a.png", "run-other/a.png", "run/../a.png", "run/./a.png", "run//a.png",
    "/run/a.png", "run/a\\b.png", "https://example.invalid/a.png", "run/a\n.png",
])
def test_freezer_and_reader_reject_unsafe_or_foreign_assets(key):
    with pytest.raises(ValueError, match="asset"):
        render([block(0, "正文", asset_keys=[key])], registered_asset_keys=[key])
    frozen, identity = render([block(0, "正文")])
    changed = changed_directory(frozen, lambda d: d["blocks"][0].update(asset_keys=[key]))
    with pytest.raises(ValueError, match="asset"):
        verify(changed, identity)


def test_freezer_rejects_unregistered_asset_and_invalid_section_type():
    with pytest.raises(ValueError, match="unregistered"):
        render([block(0, "正文", asset_keys=["run/missing.png"])])
    with pytest.raises(ValueError, match="structural list"):
        render([block(0, "正文", section_path="不得静默转换")])


def test_directory_hash_covers_new_structure_and_identity_is_strict():
    frozen, identity = render([block(0, "正文", section_path=["原章"])])
    changed = changed_directory(frozen, lambda d: d["blocks"][0].update(section_path=["改章"]))
    with pytest.raises(ValueError, match="hash"):
        verify(replace(changed, block_map_sha256=frozen.block_map_sha256), identity)
    for field in ("document_id", "parse_run_id", "source_version"):
        with pytest.raises(ValueError, match="identity"):
            verify(frozen, {**identity, field: uuid4()})


@pytest.mark.parametrize("change", [
    lambda d: d["blocks"].reverse(),
    lambda d: d["blocks"][1].update(block_id=d["blocks"][0]["block_id"]),
    lambda d: d["blocks"][1].update(block_index=d["blocks"][0]["block_index"]),
])
def test_reader_rejects_reordered_or_duplicate_block_identity(change):
    frozen, identity = render([block(0, "重复"), block(1, "重复")])
    with pytest.raises(ValueError, match="range"):
        verify(changed_directory(frozen, change), identity)
