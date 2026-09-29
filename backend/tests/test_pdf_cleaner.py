from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.ingestion.mineru.normalizer import NormalizedDocumentBlock
from app.ingestion.pdf_cleaner import (
    PdfCleaningOptions, clean_pdf_blocks, render_cleaned_block,
)
from app.ingestion.pdf_cleaner_rules import select_profile


def block(index=0, kind="text", text=None, **kwargs):
    return NormalizedDocumentBlock(
        block_index=index, block_key=f"b{index}", block_type=kind, text=text,
        page_start=kwargs.pop("page_start", 0), page_end=kwargs.pop("page_end", 0),
        bbox=kwargs.pop("bbox", [10, 100, 900, 150]), **kwargs,
    )


def clean(blocks, **kwargs):
    return clean_pdf_blocks(blocks, b"", PdfCleaningOptions(backfill_enabled=False, **kwargs))


def pdf_bytes(page_lines):
    """Small real PDF fixture, generated in memory without optional writer packages."""
    objects = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    page_ids = []
    for lines in page_lines:
        page_id, content_id = len(objects) + 1, len(objects) + 2
        page_ids.append(page_id)
        content = "\n".join(
            f"BT /F1 10 Tf 30 {y} Td ({text.replace(chr(92), chr(92) * 2).replace('(', chr(92) + '(').replace(')', chr(92) + ')')}) Tj ET"
            for y, text in lines
        ).encode("ascii")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (f"<< /Type /Pages /Kids [{' '.join(f'{n} 0 R' for n in page_ids)}] /Count {len(page_ids)} >>").encode()
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(result)


@pytest.mark.parametrize(("title", "profile"), [
    ("GB/T 43365—2023", "gb_zh"), ("ISO 9001:2015", "iso_en"),
    ("ISO/ASTM 52900:2021(en)", "iso_en"), ("JB/T 1234-2020", None),
    ("ASTM A216", None), ("COPYRIGHT PROTECTED DOCUMENT", None),
    ("This text refers to ISO 9001:2015", None),
])
def test_auto_profile_requires_standard_identity(title, profile):
    assert select_profile([block(text=title)], "auto", "paper.pdf") == profile


def test_auto_profile_filename_conflict_and_no_cross_document_state():
    assert select_profile([], "auto", "ISO 9001-2015.pdf") == "iso_en"
    assert select_profile([], "auto", "myISOstudy.pdf") is None
    assert select_profile([block(text="GB/T 1234-2020"), block(1, text="ISO 9001:2015")], "auto", "GB1234.pdf") is None
    assert select_profile([block(text="Unclassified paper")], "auto", "paper.pdf") is None
    assert select_profile([block(0, "title", "2 Normative references"), block(1, text="GB/T 43365—2023")], "auto", "paper.pdf") is None


@pytest.mark.parametrize(("profile", "names"), [
    ("gb_zh", ["目次", "前言", "引言", "参考文献"]),
    ("iso_en", ["Contents", "Foreword", "Introduction", "Bibliography"]),
])
def test_section_boundaries_keep_normative_references_and_following_annex(profile, names):
    items = []
    for name in names[:3]:
        items += [block(len(items), "title", name), block(len(items) + 1, text="REMOVE")]
    items += [block(6, "title", "1 Scope"), block(7, text="technical content"),
              block(8, "title", "2 Normative references"), block(9, text="GB/T 43365—2023"),
              block(10, "title", names[3]), block(11, text="REMOVE"),
              block(12, "title", "Annex A Technical requirements"), block(13, text="350 MPa"),
              block(14, text="450 MPa")]
    result = clean(items, profile=profile)
    assert [b.block_index for b in result] == [6, 7, 8, 9, 12, 13, 14]
    assert result[-1].section_path == ["Annex A Technical requirements"]
    assert result[2].text == "2 Normative references"


def test_unknown_profile_keeps_front_matter_and_reference_mentions():
    items = [block(0, "title", "Introduction"), block(1, text="See References for details."),
             block(2, "title", "Foreword")]
    assert len(clean(items)) == 3


def test_numbered_heading_and_annex_end_a_removed_section():
    items = [block(0, "title", "Introduction", source_metadata={"heading_level": 2}),
             block(1, "title", "Detail", source_metadata={"heading_level": 3}),
             block(2, text="REMOVE"), block(3, "title", "1 Scope", source_metadata={"heading_level": 6}),
             block(4, "title", "1.1 Application"), block(5, text="Keep")]
    result = clean(items, profile="iso_en")
    assert [b.block_index for b in result] == [3, 4, 5]
    assert result[0].markdown == "# 1 Scope"
    assert result[-1].section_path == ["1 Scope", "1.1 Application"]


def test_cover_patterns_only_apply_before_body():
    result = clean([block(0, text="ICS 77.140"), block(1, "title", "1 Scope"),
                    block(2, text="ICS 77.140"), block(3, "title", "附录 A 技术要求")], profile="gb_zh")
    assert [b.block_index for b in result] == [1, 2, 3]


@pytest.mark.parametrize("protected", [
    "<table><tr><td>Ca</td><td>Nb</td><td>R<sub>m</sub></td></tr></table>",
    '<table>\n<tr><td colspan="2"><img src="images/a.png"></td></tr>\n</table>',
    r"$$\begin{matrix}1 2 3\end{matrix}$$", r"$A \wedge B$", r"\(a \mathrm{MPa}\)",
    r"\[R_m = 350\]", "```python\n    x = 1\n```", "`a  b`",
    "| Ca | Nb |\n| -- | -- |\n| 350 | 450 |", "![caption](images/a.png)",
])
def test_protected_spans_survive_prose_cleanup(protected):
    original = f"text  before\n\n{protected}\n\ntext  after"
    result = clean([block(text=original)])[0]
    assert protected in result.text
    assert "\x00" not in result.text


def test_tables_identical_or_distinct_remain_separate_with_cell_content_intact():
    html = '<table><tr><td rowspan="2">Ca</td><td>Nb 350 MPa</td></tr></table>'
    items = [block(i, "table", html=body, markdown="stale table", caption="Table 1")
             for i, body in enumerate([html, html, html.replace("350", "450")])]
    result = clean(items)
    assert len(result) == 3
    assert [b.html for b in result] == [b.html for b in items]
    assert "stale" not in render_cleaned_block(result[0])[0]
    assert render_cleaned_block(result[0])[0] == f"Table 1\n\n{html}"


def test_formula_wrapper_only_repair_and_separate_explanation():
    latex = r"\mathrm{A} \wedge B + \begin{matrix}1 2 3\end{matrix}"
    result = clean([block(0, "formula", text=f"$${latex}$$", latex=f"$${latex}$$"),
                    block(1, "formula", text="Explanation", latex=latex)])
    assert render_cleaned_block(result[0])[0] == f"$$\n{latex}\n$$"
    assert render_cleaned_block(result[1])[0] == f"Explanation\n\n$$\n{latex}\n$$"
    assert render_cleaned_block(block(kind="formula", latex="$$broken$"))[0] == "$$broken$"
    assert render_cleaned_block(block(kind="formula", text="unknown expression"))[0] == "unknown expression"


def test_blocks_keep_sources_and_input_is_not_mutated():
    item = block(text="tensile\nstrength", asset_keys=["run/images/a.png"],
                 parent_block_key="parent", source_metadata={"mineru_block_id": "original"})
    before = asdict(item)
    result = clean([item])[0]
    assert result.text == "tensile strength"
    for name in ("block_index", "block_key", "page_start", "page_end", "bbox", "asset_keys", "parent_block_key"):
        assert getattr(result, name) == getattr(item, name)
    assert asdict(item) == before
    assert result.source_metadata is not item.source_metadata


def test_lists_code_indentation_hard_breaks_and_incomplete_math_are_kept():
    for item in [block(kind="list", text="1. keep\n2. keep"),
                 block(text="    indent\n    code"), block(text="Value $broken\nmore"),
                 block(text="keep\n  code", source_metadata={"original_type": "code"})]:
        assert clean([item])[0].text == item.text
    assert clean([block(text="first  \nsecond")])[0].text == "first  \nsecond"


def test_code_comment_is_not_a_heading_and_explicit_sections_without_titles_survive():
    item = block(text="# Introduction", source_metadata={"original_type": "code"}, section_path=["3 Example"])
    result = clean([item], profile="iso_en")
    assert len(result) == 1 and result[0].block_type == "text"
    assert result[0].text == item.text and result[0].section_path == item.section_path


def test_pipe_table_without_outside_pipes_and_protection_marker_collision_are_preserved():
    table = "Ca | Nb\n--- | ---\n350 | 450"
    assert clean([block(text=table)])[0].text == table
    text = "literal \x00PDF_SPAN_0\x00 and $R_m$"
    assert clean([block(text=text)])[0].text == text


def test_cleaning_is_idempotent_for_structures_and_protected_content():
    items = [block(0, "title", "## 1 Scope"), block(1, text="tensile\nstrength $R_m$"),
             block(2, "table", html="<table><tr><td>350 MPa</td></tr></table>"),
             block(3, "formula", latex=r"\[R_m = 350\]")]
    once = clean(items, profile="iso_en")
    twice = clean(once, profile="iso_en")
    assert [asdict(b) for b in once] == [asdict(b) for b in twice]
    assert [render_cleaned_block(b) for b in once] == [render_cleaned_block(b) for b in twice]


def test_renderer_matches_persisted_orm_shape_and_escapes_asset_paths():
    item = block(kind="image", text="Image text", caption="Diagram", asset_keys=["run/images/a b.png"])
    persisted = SimpleNamespace(**{k: v for k, v in asdict(item).items() if k != "asset_keys"})
    persisted.source_metadata["asset_keys"] = item.asset_keys
    expected = "![](images/a%20b.png)\n\nDiagram\n\nImage text"
    assert render_cleaned_block(item, output_prefix="run")[0] == expected
    assert render_cleaned_block(persisted, output_prefix="run")[0] == expected
    with pytest.raises(ValueError):
        render_cleaned_block(item, output_prefix="other-run")


def test_backfill_reads_real_pdf_text_layer_and_copies_only_missing_phrase():
    target = "The casting material must be suitable for continuous industrial operation."
    source = target.replace("be suitable", "be heat treated and suitable")
    pdf = pdf_bytes([[(650, source)]])
    result = clean_pdf_blocks([block(text=target)], pdf, PdfCleaningOptions(zero_based_pages=True))
    assert result[0].text == source
    assert result[0].bbox == [10, 100, 900, 150]
    assert result[0].block_key == "b0"


@pytest.mark.parametrize(("target_value", "source_value"), [
    ("350 MPa", "450 MPa"), ("50 MPa", "150 MPa"), ("350 Pa", "350 MPa"),
    ("shall", "shall not"),
])
def test_backfill_does_not_correct_conflicting_values_or_negation(target_value, source_value):
    template = "The casting material has {} under the specified operating conditions."
    target, source = template.format(target_value), template.format(source_value)
    result = clean_pdf_blocks([block(text=target)], pdf_bytes([[(650, source)]]), PdfCleaningOptions(zero_based_pages=True))
    assert result[0].text == target


def test_backfill_skips_ambiguous_anchors_missing_page_basis_and_empty_text():
    target = "The casting material must be suitable for continuous industrial operation."
    source = target.replace("be suitable", "be heat treated and suitable")
    for pdf, options in [
        (pdf_bytes([[(650, source), (630, source)]]), PdfCleaningOptions(zero_based_pages=True)),
        (pdf_bytes([[(650, source)]]), PdfCleaningOptions()),
        (pdf_bytes([[]]), PdfCleaningOptions(zero_based_pages=True)),
    ]:
        assert clean_pdf_blocks([block(text=target)], pdf, options)[0].text == target


def test_backfill_is_same_block_same_page_and_can_be_disabled():
    target = "The casting material must be suitable for continuous industrial operation."
    source = target.replace("be suitable", "be heat treated and suitable")
    pdf = pdf_bytes([[(650, source)], [(650, target)]])
    items = [block(0, text=target, page_start=1, page_end=1), block(1, text=target, page_end=1)]
    result = clean_pdf_blocks(items, pdf, PdfCleaningOptions(zero_based_pages=True))
    assert all(b.text == target for b in result)
    assert clean_pdf_blocks([block(text=target)], pdf, PdfCleaningOptions(zero_based_pages=True, backfill_enabled=False))[0].text == target


def test_backfill_does_not_borrow_another_block_or_reintroduce_a_filtered_footer():
    target = "The casting material must be suitable for continuous industrial operation."
    source = target.replace("be suitable", "be heat treated and suitable")
    pdf = pdf_bytes([[(650, source)]])
    for kind in ("text", "footer"):
        items = [block(text=target), block(1, kind, "heat treated and")]
        result = clean_pdf_blocks(items, pdf, PdfCleaningOptions(zero_based_pages=True))
        assert result[0].text == target


def test_running_standard_requires_repeated_physical_margin_and_never_deletes_body_reference():
    identifier = "GB/T 43365-2023"
    pdf = pdf_bytes([[(765, identifier), (650, "Body")], [(765, identifier), (650, "Body")],
                     [(765, identifier), (650, identifier)]])
    items = [block(i, text=identifier, page_start=i, page_end=i) for i in range(3)]
    items += [block(3, text=identifier, page_start=2, page_end=2)]
    result = clean_pdf_blocks(items, pdf, PdfCleaningOptions(profile="gb_zh", zero_based_pages=True))
    assert [b.block_index for b in result] == [2, 3]


def test_settings_defaults_and_profile_validation():
    settings = Settings(_env_file=None)
    assert settings.pdf_cleaning_enabled is False
    assert settings.pdf_cleaning_profile == "auto"
    assert settings.pdf_cleaning_backfill_enabled is True
    with pytest.raises(ValueError):
        Settings(_env_file=None, pdf_cleaning_profile="casting_domain")
