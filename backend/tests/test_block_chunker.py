"""S2 pure interval tests. Synthetic frozen sources; no services, DB or models."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import random
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.ingestion.block_chunker import (
    BlockChunkerConfig, SEGMENTATION_VERSION, build_block_aware_chunks,
    _Interval, _merge_small_tail,
)
from app.ingestion.frozen_source import json_bytes, sha256_bytes
from app.ingestion.source_intervals import overlaps
from pdf_source_fixtures import frozen_blocks

DEFAULTS = dict(max_chunk_chars=1800, min_chunk_chars=200, overlap_chars=0,
                max_table_chars=4000, keep_table_intact=True, keep_formula_with_context=True)


def build(values, config=None, **kwargs):
    source, identity = frozen_blocks(values)
    result = build_block_aware_chunks(
        source, document_id=identity["document_id"], parse_run_id=identity["parse_run_id"],
        output_prefix="run", config=config, **kwargs,
    )
    check_intervals(source, result, config or BlockChunkerConfig())
    return result, source


def check_intervals(source, result, config):
    text = source.canonical.decode("utf-8")
    entries = json.loads(source.block_map)["blocks"]
    covered = 0
    previous_start, previous_end = -1, -1
    for chunk in result.chunks:
        start, end = chunk.source_start, chunk.source_end
        assert 0 <= start < end <= len(text)
        assert start > previous_start and end > previous_end
        assert start <= covered
        assert chunk.source_version == source.source_version
        assert chunk.content == text[start:end] and chunk.content.strip()
        assert chunk.content_sha256 == sha256_bytes(chunk.content.encode("utf-8"))
        matched = [b for b in entries if overlaps(start, end, b["source_start"], b["source_end"])]
        assert chunk.source_metadata["block_ids"] == [b["block_id"] for b in matched]
        assert all(b["section_path"] == matched[0]["section_path"] for b in matched)
        if all(b["block_type"] not in {"table", "formula"} for b in matched):
            # Only source whitespace may account for an ordinary chunk's overrun.
            assert len(chunk.content.strip()) <= config.max_chunk_chars
        for b in matched:
            if b["block_type"] == "formula" or (b["block_type"] == "table" and config.keep_table_intact):
                assert start <= b["source_start"] < b["source_end"] <= end
        covered = max(covered, end)
        previous_start, previous_end = start, end
    assert result.chunks[0].source_start == 0 and covered == len(text)


def test_six_defaults_normalization_and_fingerprint_do_not_read_global_settings(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_CHARS", "1000")
    monkeypatch.setenv("CHUNK_OVERLAP_CHARS", "100")
    default = BlockChunkerConfig()
    explicit = BlockChunkerConfig.model_validate(DEFAULTS)
    reordered = BlockChunkerConfig.model_validate(dict(reversed(list(DEFAULTS.items()))))
    assert set(BlockChunkerConfig.model_fields) == set(DEFAULTS)
    assert default.model_dump() == DEFAULTS and default == explicit == reordered
    assert default.fingerprint == explicit.fingerprint == reordered.fingerprint
    assert default.fingerprint == sha256_bytes(json_bytes(DEFAULTS))
    assert BlockChunkerConfig(max_chunk_chars=3000).fingerprint != default.fingerprint
    assert build([dict(text="正文" * 1000)])[0] == build([dict(text="正文" * 1000)], explicit)[0]


@pytest.mark.parametrize("updates", [
    dict(max_chunk_chars=0), dict(max_chunk_chars=-1), dict(max_chunk_chars=60001), dict(max_table_chars=0),
    dict(max_table_chars=-1), dict(min_chunk_chars=-1), dict(overlap_chars=-1),
    dict(min_chunk_chars=1801), dict(overlap_chars=1800), dict(overlap_chars=1801),
    dict(max_chunk_chars=100),  # Default min=200 must NOT be clamped.
    dict(chunk_size=1000), dict(overlap=100), dict(boundary="line"),
    dict(include_headers_footers=False), dict(unknown=True),
])
def test_invalid_or_legacy_config_rejected(updates):
    with pytest.raises(ValidationError):
        BlockChunkerConfig(**updates)


def test_max_chunk_chars_60000_inclusive_cap():
    assert BlockChunkerConfig(max_chunk_chars=60000).max_chunk_chars == 60000


@pytest.mark.parametrize("name", ["max_chunk_chars", "min_chunk_chars", "overlap_chars", "max_table_chars"])
@pytest.mark.parametrize("value", ["200", 200.0, True, None])
def test_numeric_types_are_not_coerced(name, value):
    with pytest.raises(ValidationError):
        BlockChunkerConfig(**{name: value})


@pytest.mark.parametrize("name", ["keep_table_intact", "keep_formula_with_context"])
@pytest.mark.parametrize("value", ["true", 1, 0, None])
def test_boolean_types_are_not_coerced(name, value):
    with pytest.raises(ValidationError):
        BlockChunkerConfig(**{name: value})


def test_config_is_frozen_and_json_roundtrip_is_deterministic():
    config = BlockChunkerConfig(max_chunk_chars=1200, min_chunk_chars=200, overlap_chars=120,
                                max_table_chars=4000, keep_table_intact=True, keep_formula_with_context=True)
    assert config == BlockChunkerConfig.model_validate_json(config.model_dump_json())
    with pytest.raises(ValidationError):
        config.overlap_chars = 0


@pytest.mark.parametrize("field,value", [
    ("max_chunk_chars", 3000), ("min_chunk_chars", 0), ("overlap_chars", 120),
    ("max_table_chars", 3000), ("keep_table_intact", False), ("keep_formula_with_context", False),
])
def test_each_effective_configuration_field_is_part_of_fingerprint(field, value):
    assert BlockChunkerConfig(**{field: value}).fingerprint != BlockChunkerConfig().fingerprint


def test_splitter_uses_frozen_directory_without_calling_renderer_or_mutable_blocks(monkeypatch):
    source, identity = frozen_blocks([dict(text="正文", section_path=["冻结章"])])
    before = asdict(source)
    monkeypatch.setattr("app.ingestion.frozen_source.render_cleaned_block",
                        lambda *a, **k: pytest.fail("splitter must not render again"))
    result = build_block_aware_chunks(source, document_id=identity["document_id"],
                                     parse_run_id=identity["parse_run_id"], output_prefix="run")
    assert result.chunks[0].section_title == "冻结章" and asdict(source) == before


def test_structure_decisions_match_pre_s2_rules_with_explicit_six_field_config():
    # Captured from HEAD's old block_chunker using the same cleaned renderer and
    # six settings. New intervals additionally own the existing trailing separators.
    config = BlockChunkerConfig(max_chunk_chars=1200, min_chunk_chars=200, overlap_chars=120,
                                max_table_chars=4000, keep_table_intact=True, keep_formula_with_context=True)
    result, _ = build([
        dict(block_type="title", text="Chapter A", section_path=["A"]),
        dict(text="Intro"),
        dict(block_type="table", text="T" * 3455),
        dict(text="After table"),
        dict(block_type="formula", text="F" * 1607),
        dict(block_type="title", text="Chapter B", section_path=["B"]),
        dict(text="Tail"),
    ], config)
    assert [(c.source_metadata["block_keys"], c.chunk_type, c.content_format,
             len(c.content.removesuffix("\n\n"))) for c in result.chunks] == [
        (["b0", "b1"], "text", "mixed", 16),
        (["b2"], "table", "markdown", 3455),
        (["b3"], "text", "plain_text", 11),
        (["b4"], "formula", "plain_text", 1607),
        (["b5", "b6"], "text", "mixed", 15),
    ]


def test_sections_titles_lists_and_page_boundaries_use_frozen_metadata():
    values = [
        dict(block_type="title", text="重复标题", section_path=["章一"], page_start=0, page_end=0),
        dict(text="正文", page_start=0, page_end=1),
        dict(block_type="list", text="1. 条件\n2. 检查", page_start=1, page_end=1),
        dict(block_type="title", text="重复标题", section_path=["章二"], page_start=2, page_end=2),
        dict(text="正文", page_start=2, page_end=2),
    ]
    result, source = build(values)
    assert len(result.chunks) == 2
    first, second = result.chunks
    assert first.section_title == "章一" and second.section_title == "章二"
    assert first.source_metadata["block_keys"] == ["b0", "b1", "b2"]
    assert first.content_format == "mixed"  # Title renderer markdown + plain body.
    assert (first.page_start, first.page_end) == (0, 1)
    assert first.source_end == second.source_start
    assert first.content.endswith("\n\n")  # Existing inter-block separator belongs left.
    assert first.content + second.content == source.canonical.decode("utf-8")


@pytest.mark.parametrize("length", [2500, 6000])
def test_default_intact_tables_can_exceed_both_body_and_table_targets(length):
    table = "表" * length
    result, _ = build([dict(text="之前"), dict(block_type="table", text=table), dict(text="之后")])
    assert [c.chunk_type for c in result.chunks] == ["text", "table", "text"]
    assert result.chunks[1].content == table + "\n\n"


def test_false_small_table_uses_table_limit_not_body_limit():
    result, _ = build([dict(block_type="table", text="表" * 2500)],
                       BlockChunkerConfig(keep_table_intact=False))
    assert len(result.chunks) == 1 and len(result.chunks[0].content) == 2500


def test_false_long_table_groups_complete_physical_rows_above_body_target():
    rows = ["行" * 1749 + "\n"] * 5
    result, source = build([dict(block_type="table", text="".join(rows))],
                           BlockChunkerConfig(keep_table_intact=False))
    assert [len(c.content) for c in result.chunks] == [3500, 3500, 1749]
    assert "".join(c.content for c in result.chunks) == source.canonical.decode("utf-8")
    assert all(c.source_start % 1750 == 0 for c in result.chunks)
    assert all(c.source_end % 1750 == 0 for c in result.chunks[:-1])


def test_false_oversized_row_and_blank_lines_remain_complete():
    rows = ["短行\n", "大" * 5000 + "\n", "\n", "末行"]
    result, source = build([dict(block_type="table", text="".join(rows))],
                           BlockChunkerConfig(keep_table_intact=False, overlap_chars=100))
    assert [c.content for c in result.chunks] == ["短行\n", "大" * 5000 + "\n\n", "末行"]
    assert "".join(c.content for c in result.chunks) == source.canonical.decode("utf-8")


def test_table_title_merge_only_for_adjacent_titles_with_real_capacity():
    result, _ = build([
        dict(block_type="title", text="标题"),
        dict(block_type="table", text="小表"),
        dict(text="后文"),
        dict(block_type="table", text="第二表"),
    ])
    assert [c.source_metadata["block_keys"] for c in result.chunks] == [["b0", "b1"], ["b2"], ["b3"]]
    assert result.chunks[0].chunk_type == "table"
    limited, _ = build([dict(block_type="title", text="标题"),
                         dict(block_type="table", text="表" * 1798)])
    assert len(limited.chunks) == 2  # 2 title + 2 existing separator + 1798 table > 1800.
    long_table, _ = build([dict(block_type="title", text="标题"),
                            dict(block_type="table", text="表" * 2500)],
                           BlockChunkerConfig(keep_table_intact=False))
    assert len(long_table.chunks) == 2 and long_table.chunks[1].chunk_type == "table"


@pytest.mark.parametrize("merge,expected", [(True, 1), (False, 3)])
def test_short_formula_switch_controls_merging_not_formula_integrity(merge, expected):
    result, _ = build([dict(text="解释"), dict(block_type="formula", latex="Q = mcT"), dict(text="条件")],
                       BlockChunkerConfig(keep_formula_with_context=merge))
    assert len(result.chunks) == expected
    assert "$$\nQ = mcT\n$$" in "".join(c.content for c in result.chunks)
    assert result.chunks[0 if merge else 1].chunk_type == ("mixed" if merge else "formula")


@pytest.mark.parametrize("merge", [True, False])
def test_formula_over_1800_is_independent_and_never_split(merge):
    result, _ = build([dict(text="前文"), dict(block_type="formula", latex="x" * 2000), dict(text="后文")],
                       BlockChunkerConfig(keep_formula_with_context=merge, overlap_chars=150))
    assert len(result.chunks) == 3
    assert result.chunks[1].content == "$$\n" + "x" * 2000 + "\n$$\n\n"


def test_1607_character_formula_is_not_oversized_under_default_1800():
    formula = "$$\n" + "x" * 1601 + "\n$$"
    assert len(formula) == 1607
    result, _ = build([dict(text="解释"), dict(block_type="formula", text=formula), dict(text="条件")])
    assert len(result.chunks) == 1 and result.chunks[0].chunk_type == "mixed"


def test_formula_merge_capacity_and_section_boundary():
    config = BlockChunkerConfig(max_chunk_chars=20, min_chunk_chars=0)
    result, _ = build([dict(text="x" * 18), dict(block_type="formula", text="F"),
                       dict(text="解释", section_path=["新章"])], config)
    assert len(result.chunks) == 3
    assert result.chunks[1].chunk_type == "formula"


@pytest.mark.parametrize("minimum", [0, 200])
def test_small_structural_chunks_are_not_forced_to_minimum(minimum):
    result, _ = build([
        dict(block_type="title", text="短章"),
        dict(block_type="title", text="次章"),
        dict(block_type="table", text="小表"),
        dict(block_type="formula", text="小公式"),
        dict(text="尾文", section_path=["末章"]),
    ], BlockChunkerConfig(min_chunk_chars=minimum, keep_formula_with_context=False))
    assert len(result.chunks) == 4 and all(len(c.content) < 200 for c in result.chunks)


def test_small_tail_helper_only_merges_last_contiguous_pair_that_fits():
    parts = [_Interval(0, 400), _Interval(400, 500)]
    assert _merge_small_tail(parts, BlockChunkerConfig()) == [_Interval(0, 500)]
    assert _merge_small_tail(parts, BlockChunkerConfig(min_chunk_chars=0)) == parts
    full = [_Interval(0, 1750), _Interval(1750, 1850)]
    gap = [_Interval(0, 400), _Interval(401, 500)]
    assert _merge_small_tail(full, BlockChunkerConfig()) == full
    assert _merge_small_tail(gap, BlockChunkerConfig()) == gap


def test_paragraph_whitespace_hard_boundaries_and_small_tail_retention():
    result, _ = build([dict(text="甲" * 40 + "\n\n" + "乙" * 40 + "\n\n" + "尾" * 10)],
                       BlockChunkerConfig(max_chunk_chars=50, min_chunk_chars=20))
    assert [len(c.content) for c in result.chunks] == [42, 42, 10]
    assert result.chunks[-1].content == "尾" * 10  # 42+10 exceeds max; no forced merge.
    spaced, _ = build([dict(text="abc " * 25)], BlockChunkerConfig(max_chunk_chars=15, min_chunk_chars=0))
    assert all(c.content.endswith(" ") for c in spaced.chunks)


def test_overlap_is_best_effort_and_full_chunks_can_have_zero_overlap():
    result, _ = build([dict(text="文" * 3600)], BlockChunkerConfig(overlap_chars=100))
    assert [(c.source_start, c.source_end) for c in result.chunks] == [(0, 1800), (1800, 3600)]
    tail, _ = build([dict(text="文" * 3700)], BlockChunkerConfig(overlap_chars=100))
    assert [(c.source_start, c.source_end) for c in tail.chunks] == [(0, 1800), (1800, 3600), (3500, 3700)]


def test_overlap_reduces_to_remaining_capacity_without_inserting_separators():
    result, source = build([dict(text="前" * 60 + "\n\n" + "后" * 95)],
                           BlockChunkerConfig(max_chunk_chars=100, min_chunk_chars=0, overlap_chars=20))
    first, second = result.chunks
    assert first.source_end == 62
    # The permitted suffix has a whitespace boundary at 62, so no word fragment is forced.
    assert second.source_start >= 57
    assert len(second.content) <= 100
    assert second.content == source.canonical.decode("utf-8")[second.source_start:second.source_end]
    capacity, _ = build([dict(text="字" * 295)],
                        BlockChunkerConfig(max_chunk_chars=100, min_chunk_chars=0, overlap_chars=20))
    assert capacity.chunks[-1].source_start == 195  # Only 5 characters of room.


def test_overlap_never_reaches_tables_formulas_other_blocks_or_sections():
    result, source = build([
        dict(block_type="table", text="表" * 2000),
        dict(text="文" * 3700),
        dict(block_type="formula", text="式" * 2000),
        dict(text="文" * 3700, section_path=["另一章"]),
    ], BlockChunkerConfig(overlap_chars=100))
    entries = json.loads(source.block_map)["blocks"]
    for chunk in result.chunks:
        if len(chunk.source_metadata["block_ids"]) == 1:
            b = next(b for b in entries if b["block_id"] == chunk.source_metadata["block_ids"][0])
            assert chunk.source_start >= b["source_start"]
            assert chunk.source_end <= b["source_end"] + 2


def test_unicode_repeated_text_indentation_and_empty_whitespace_fragments():
    value = "\n\n" + "e\u0301😀重复\t  " * 70 + "\n" * 30 + "尾"
    result, source = build([dict(text=value), dict(text=value)],
                           BlockChunkerConfig(max_chunk_chars=7, min_chunk_chars=0, overlap_chars=3))
    assert "é😀" in source.canonical.decode("utf-8")
    assert all(c.content.strip() for c in result.chunks)
    smallest, raw = build([dict(text=" \t\n  中  \n  文\n\n")],
                          BlockChunkerConfig(max_chunk_chars=1, min_chunk_chars=0))
    assert "".join(c.content for c in smallest.chunks) == raw.canonical.decode("utf-8")


def test_metadata_links_and_copies_are_deterministic():
    values = [
        dict(block_type="title", text="章", section_path=["章"], page_start=0, page_end=0),
        dict(text="条件", asset_keys=["run/b.png", "run/a.png"], page_start=1, page_end=2),
    ]
    original = deepcopy(values)
    first, source = build(values)
    second, _ = build(values)
    assert asdict(first) == asdict(second) and values == original
    chunk = first.chunks[0]
    assert chunk.chunk_method == SEGMENTATION_VERSION
    assert chunk.source_metadata["asset_keys"] == ["run/b.png", "run/a.png"]
    assert chunk.source_metadata["block_types"] == ["title", "text"]
    assert (chunk.page_start, chunk.page_end) == (0, 2)
    assert chunk.content_format == "mixed"
    chunk.source_metadata["asset_keys"].clear()
    assert json.loads(source.block_map)["blocks"][1]["asset_keys"] == ["run/b.png", "run/a.png"]
    assert second.chunks[0].source_metadata["asset_keys"] == ["run/b.png", "run/a.png"]


def test_image_caption_and_unknown_content_use_s1_rendering():
    result, source = build([dict(block_type="header", text="不进入canonical"),
                            dict(block_type="image", caption="图注", asset_keys=["run/a.png"]),
                            dict(block_type="caption", text="说明")])
    assert len(result.chunks) == 1 and result.chunks[0].chunk_type == "image_caption"
    assert "![](a.png)" in result.chunks[0].content
    assert "不进入" not in source.canonical.decode("utf-8")
    plain, _ = build([dict(block_type="unknown", text="保留")])
    assert plain.chunks[0].content == "保留"


@pytest.mark.parametrize("length", [254, 255, 256, 300])
@pytest.mark.parametrize("alphabet", ["章节", "😀𝄞", "中é😀"])
def test_section_title_display_projection_preserves_full_source(length, alphabet):
    title = (alphabet * length)[:length]
    path = ["完整父章节" * 60, title]
    values = [dict(block_type="title", text=title, section_path=path), dict(text="原始正文😀")]
    before = deepcopy(values)
    result, source = build(values)
    again, same_source = build(values)
    chunk, = result.chunks
    assert len(title) == length and len(title.encode("utf-8")) > length
    assert chunk.section_title == title[:255]
    assert len(chunk.section_title) == min(length, 255)
    if length <= 255:
        assert chunk.section_title == title
    assert chunk.source_metadata["section_path"] == path
    assert all(entry["section_path"] == path for entry in json.loads(source.block_map)["blocks"])
    assert chunk.content == source.canonical.decode("utf-8") == title + "\n\n原始正文😀"
    assert (chunk.source_start, chunk.source_end) == (0, source.character_count)
    assert chunk.content_sha256 == sha256_bytes(source.canonical)
    assert result == again and source == same_source and values == before


def test_section_title_projection_does_not_reformat_or_fill_missing_path():
    title = " \t章😀\n" + "长" * 300
    result, _ = build([dict(text="正文", section_path=[title])])
    assert result.chunks[0].section_title == title[:255]
    assert result.chunks[0].source_metadata["section_path"] == [title]
    no_path, _ = build([dict(text="正文")])
    assert no_path.chunks[0].section_title is None
    assert no_path.chunks[0].source_metadata["section_path"] == []


@pytest.mark.parametrize("version", ["sequential-codepoints-v1", "unknown", None])
def test_unsupported_segmentation_versions_rejected(version):
    with pytest.raises(ValueError, match="Unsupported"):
        build([dict(text="正文")], segmentation_version=version)


@pytest.mark.parametrize("change", [
    lambda d: d.update(schema_version=1),
    lambda d: d["blocks"][0].pop("section_path"),
    lambda d: d["blocks"][0].pop("content_format"),
    lambda d: d["blocks"][0].pop("asset_keys"),
    lambda d: d["blocks"][0].update(source_end=9999),
    lambda d: d["blocks"][0].update(section_path="not a list"),
    lambda d: d["blocks"][0].update(section_path=[123]),
])
def test_invalid_source_map_never_falls_back_to_text_splitting(change):
    source, identity = frozen_blocks([dict(text="正文")])
    directory = json.loads(source.block_map)
    change(directory)
    raw = json_bytes(directory)
    source = replace(source, block_map=raw, block_map_sha256=sha256_bytes(raw))
    with pytest.raises(ValueError):
        build_block_aware_chunks(source, document_id=identity["document_id"],
                                 parse_run_id=identity["parse_run_id"], output_prefix="run")


def test_source_bytes_identity_and_chunk_budget_are_checked():
    source, identity = frozen_blocks([dict(text="字" * 4000)])
    for changed, doc in [(replace(source, canonical=b"changed"), identity["document_id"]),
                         (source, UUID(int=999))]:
        with pytest.raises(ValueError):
            build_block_aware_chunks(changed, document_id=doc, parse_run_id=identity["parse_run_id"],
                                     output_prefix="run")
    with pytest.raises(ValueError, match="budget exceeded"):
        build([dict(text="字" * 4000)], max_chunks=1)


@pytest.mark.parametrize("bounds,expected", [
    ((0, 5, 5, 8), False), ((5, 8, 0, 5), False), ((0, 8, 2, 3), True),
    ((2, 3, 0, 8), True), ((0, 3, 2, 5), True), ((0, 0, 0, 5), False),
])
def test_shared_overlap_preserves_strict_half_open_semantics(bounds, expected):
    assert overlaps(*bounds) is expected


def test_seeded_mixed_structures_cover_all_codepoints_and_protect_both_ends():
    rng = random.Random(417)
    for _ in range(60):
        values = []
        for i in range(rng.randint(2, 10)):
            kind = rng.choice(["text", "title", "list", "table", "formula"])
            text = "".join(rng.choice("甲乙😀 \t\n") for _ in range(rng.randint(1, 150))) + "末"
            values.append(dict(block_type=kind, text=text,
                               section_path=[f"章{i // 3}"], page_start=i // 2, page_end=i // 2))
        maximum = rng.randint(1, 100)
        config = BlockChunkerConfig(max_chunk_chars=maximum, min_chunk_chars=rng.randint(0, maximum),
                                    overlap_chars=rng.randrange(maximum), max_table_chars=rng.randint(1, 120),
                                    keep_table_intact=rng.choice([True, False]),
                                    keep_formula_with_context=rng.choice([True, False]))
        result, source = build(values, config)
        if config.overlap_chars == 0:
            assert "".join(c.content for c in result.chunks) == source.canonical.decode("utf-8")
