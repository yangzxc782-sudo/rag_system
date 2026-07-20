from __future__ import annotations

from app.ingestion.block_chunker import (
    BlockChunkerConfig,
    build_block_aware_chunks,
)
from app.ingestion.mineru.normalizer import NormalizedDocumentBlock

PARSE_RUN_ID = "22222222-2222-2222-2222-222222222222"


def _block(
    block_index: int,
    block_type: str,
    text: str | None = None,
    *,
    block_key: str | None = None,
    page_start: int | None = 1,
    page_end: int | None = None,
    markdown: str | None = None,
    latex: str | None = None,
    caption: str | None = None,
    section_path: list[str] | None = None,
    asset_keys: list[str] | None = None,
    block_id: str | None = None,
) -> NormalizedDocumentBlock:
    source_metadata = {"mineru_block_id": f"mineru-{block_index}"}
    if block_id is not None:
        source_metadata["document_block_id"] = block_id
    return NormalizedDocumentBlock(
        block_index=block_index,
        block_key=block_key or f"block-{block_index}",
        block_type=block_type,
        page_start=page_start,
        page_end=page_end if page_end is not None else page_start,
        text=text,
        markdown=markdown,
        latex=latex,
        caption=caption,
        section_path=section_path or [],
        asset_keys=asset_keys or [],
        source_metadata=source_metadata,
    )


def test_title_text_and_list_merge_within_the_same_section() -> None:
    blocks = [
        _block(0, "title", "Casting", section_path=["Casting"]),
        _block(1, "text", "Prepare the mould.", section_path=["Casting"]),
        _block(2, "list", "1. Dry\n2. Inspect", section_path=["Casting"]),
    ]

    result = build_block_aware_chunks(
        blocks,
        parse_run_id=PARSE_RUN_ID,
        config=BlockChunkerConfig(max_chunk_chars=500),
    )

    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.content == "Casting\n\nPrepare the mould.\n\n1. Dry\n2. Inspect"
    assert chunk.section_title == "Casting"
    assert chunk.source_metadata["section_path"] == ["Casting"]
    assert chunk.chunk_type == "text"
    assert chunk.content_format == "plain_text"
    assert [link.block_order for link in result.links] == [0, 1, 2]


def test_section_change_starts_a_new_chunk() -> None:
    blocks = [
        _block(0, "title", "Preparation", section_path=["Preparation"]),
        _block(1, "text", "Prepare.", section_path=["Preparation"]),
        _block(2, "title", "Pouring", section_path=["Pouring"]),
        _block(3, "text", "Pour.", section_path=["Pouring"]),
    ]

    result = build_block_aware_chunks(
        blocks,
        parse_run_id=PARSE_RUN_ID,
        config=BlockChunkerConfig(max_chunk_chars=500),
    )

    assert [chunk.section_title for chunk in result.chunks] == [
        "Preparation",
        "Pouring",
    ]
    assert result.chunks[0].source_metadata["block_keys"] == [
        "block-0",
        "block-1",
    ]
    assert result.chunks[1].source_metadata["block_keys"] == [
        "block-2",
        "block-3",
    ]


def test_table_is_kept_intact_and_long_table_is_an_independent_chunk() -> None:
    table = "|A|B|\n|-|-|\n" + "\n".join(f"|{i}|{i + 1}|" for i in range(20))
    blocks = [
        _block(0, "text", "Before table.", section_path=["Data"]),
        _block(
            1,
            "table",
            markdown=table,
            section_path=["Data"],
            page_start=2,
        ),
        _block(2, "text", "After table.", section_path=["Data"], page_start=3),
    ]

    result = build_block_aware_chunks(
        blocks,
        parse_run_id=PARSE_RUN_ID,
        config=BlockChunkerConfig(
            max_chunk_chars=80,
            min_chunk_chars=20,
            max_table_chars=100,
            keep_table_intact=True,
        ),
    )

    table_chunks = [chunk for chunk in result.chunks if chunk.chunk_type == "table"]
    assert len(table_chunks) == 1
    assert table_chunks[0].content == table
    assert table_chunks[0].content_format == "markdown"
    assert table_chunks[0].source_metadata["block_keys"] == ["block-1"]


def test_formula_stays_adjacent_to_explanation_text() -> None:
    blocks = [
        _block(0, "text", "Heat is calculated as follows.", section_path=["Heat"]),
        _block(
            1,
            "formula",
            text="Heat equation",
            latex="Q = mc\\Delta T",
            section_path=["Heat"],
        ),
        _block(2, "text", "Use SI units.", section_path=["Heat"]),
    ]

    result = build_block_aware_chunks(
        blocks,
        parse_run_id=PARSE_RUN_ID,
        config=BlockChunkerConfig(max_chunk_chars=500),
    )

    assert len(result.chunks) == 1
    assert "Heat is calculated" in result.chunks[0].content
    assert "Q = mc\\Delta T" in result.chunks[0].content
    assert "Use SI units" in result.chunks[0].content
    assert result.chunks[0].chunk_type == "mixed"
    assert result.chunks[0].content_format == "mixed"


def test_long_formula_is_kept_intact_in_an_independent_chunk() -> None:
    latex = (
        r"Q_{\mathrm{total}} = "
        r"\sum_{i=1}^{n} m_i c_i "
        r"\left(T_{\mathrm{pour}} - T_{\mathrm{mould}}\right)"
    )
    blocks = [
        _block(
            0,
            "text",
            "The heat balance is calculated before pouring.",
            section_path=["Heat"],
        ),
        _block(
            1,
            "formula",
            latex=latex,
            section_path=["Heat"],
            block_id="db-formula-1",
        ),
        _block(
            2,
            "text",
            "The result is checked against the process limit.",
            section_path=["Heat"],
        ),
    ]

    result = build_block_aware_chunks(
        blocks,
        parse_run_id=PARSE_RUN_ID,
        config=BlockChunkerConfig(
            max_chunk_chars=60,
            min_chunk_chars=20,
        ),
    )

    assert len(result.chunks) == 3
    formula_chunk = result.chunks[1]
    assert formula_chunk.chunk_type == "formula"
    assert formula_chunk.content == f"$$\n{latex}\n$$"
    assert len(formula_chunk.content) > 60
    assert formula_chunk.source_metadata["block_keys"] == ["block-1"]
    assert formula_chunk.source_metadata["block_ids"] == ["db-formula-1"]
    assert formula_chunk.source_metadata["block_types"] == ["formula"]

    formula_links = [
        link
        for link in result.links
        if link.chunk_index == formula_chunk.chunk_index
    ]
    assert len(formula_links) == 1
    assert formula_links[0].block_id == "db-formula-1"
    assert formula_links[0].block_key == "block-1"
    assert formula_links[0].block_order == 0
    assert result.chunks[0].content == (
        "The heat balance is calculated before pouring."
    )
    assert result.chunks[2].content == (
        "The result is checked against the process limit."
    )


def test_image_and_caption_create_an_image_caption_chunk() -> None:
    asset_key = f"parsed-assets/document/{PARSE_RUN_ID}/images/mould.png"
    blocks = [
        _block(
            0,
            "image",
            caption="Mould layout",
            section_path=["Layout"],
            asset_keys=[asset_key],
        ),
        _block(
            1,
            "caption",
            text="Figure 1: Mould layout",
            section_path=["Layout"],
        ),
    ]

    result = build_block_aware_chunks(
        blocks,
        parse_run_id=PARSE_RUN_ID,
        config=BlockChunkerConfig(max_chunk_chars=500),
    )

    assert len(result.chunks) == 1
    assert result.chunks[0].chunk_type == "image_caption"
    assert result.chunks[0].source_metadata["asset_keys"] == [asset_key]
    assert "Mould layout" in result.chunks[0].content


def test_headers_and_footers_are_filtered_and_unknown_is_kept() -> None:
    blocks = [
        _block(0, "header", "Casting handbook"),
        _block(1, "unknown", "Conservative fallback text."),
        _block(2, "footer", "Page 1"),
    ]

    result = build_block_aware_chunks(
        blocks,
        parse_run_id=PARSE_RUN_ID,
        config=BlockChunkerConfig(max_chunk_chars=500),
    )

    assert len(result.chunks) == 1
    assert result.chunks[0].content == "Conservative fallback text."
    assert result.chunks[0].source_metadata["block_types"] == ["unknown"]


def test_page_range_and_source_metadata_are_aggregated() -> None:
    asset_key = f"parsed-assets/document/{PARSE_RUN_ID}/images/detail.png"
    blocks = [
        _block(
            0,
            "title",
            "Defects",
            page_start=2,
            section_path=["Defects"],
        ),
        _block(
            1,
            "text",
            "Inspect the surface.",
            page_start=2,
            page_end=3,
            section_path=["Defects"],
            asset_keys=[asset_key],
        ),
    ]

    result = build_block_aware_chunks(
        blocks,
        parse_run_id=PARSE_RUN_ID,
        config=BlockChunkerConfig(max_chunk_chars=500),
    )
    chunk = result.chunks[0]

    assert chunk.page_start == 2
    assert chunk.page_end == 3
    assert chunk.parse_run_id == PARSE_RUN_ID
    assert chunk.chunk_method == "mineru_block_merge"
    assert chunk.source_metadata == {
        "parser_provider": "mineru_api",
        "parse_run_id": PARSE_RUN_ID,
        "block_ids": [],
        "block_keys": ["block-0", "block-1"],
        "block_types": ["title", "text"],
        "asset_keys": [asset_key],
        "section_path": ["Defects"],
        "chunk_method": "mineru_block_merge",
        "content_format": "plain_text",
    }


def test_long_text_is_split_at_paragraph_boundaries() -> None:
    text = (
        "First paragraph has enough detail for its own chunk."
        "\n\nSecond paragraph also contains useful process detail."
        "\n\nThird paragraph completes the explanation."
    )
    blocks = [_block(0, "text", text, section_path=["Long section"])]

    result = build_block_aware_chunks(
        blocks,
        parse_run_id=PARSE_RUN_ID,
        config=BlockChunkerConfig(
            max_chunk_chars=70,
            min_chunk_chars=20,
            overlap_chars=0,
        ),
    )

    assert len(result.chunks) == 3
    assert all(len(chunk.content) <= 70 for chunk in result.chunks)
    assert all(
        chunk.source_metadata["block_keys"] == ["block-0"]
        for chunk in result.chunks
    )
