from __future__ import annotations

import pytest

from app.ingestion.mineru.models import (
    MinerUAssetResult,
    MinerUParseResult,
    MinerUResultFile,
)
from app.ingestion.mineru.normalizer import (
    MinerUNormalizationError,
    normalize_mineru_result,
)

DOCUMENT_ID = "11111111-1111-1111-1111-111111111111"
PARSE_RUN_ID = "22222222-2222-2222-2222-222222222222"


def _parse_result() -> MinerUParseResult:
    return MinerUParseResult(
        parser_name="mineru_api",
        parser_version="test-v1",
        parse_mode="auto",
        markdown_text="# Casting",
        text="Casting process",
        content_list=(
            {
                "type": "title",
                "id": "title-1",
                "level": 1,
                "text": "Casting",
                "page_start": 1,
                "bbox": [10, 20, 300, 60],
                "ignored_large_payload": "do-not-copy",
            },
            {
                "type": "paragraph",
                "id": "text-1",
                "text": "Prepare the mould.",
                "page": 1,
            },
            {
                "type": "list",
                "id": "list-1",
                "text": "1. Dry\n2. Inspect",
                "page": 1,
            },
            {
                "type": "table",
                "id": "table-1",
                "markdown": "|A|B|\n|-|-|\n|1|2|",
                "html": "<table><tr><td>1</td></tr></table>",
                "page_start": 2,
                "page_end": 2,
            },
            {
                "type": "equation",
                "id": "formula-1",
                "latex": "Q = mc\\Delta T",
                "text": "Heat equation",
                "page": 2,
            },
            {
                "type": "figure",
                "id": "image-1",
                "caption": "Mould layout",
                "asset_key": "images/mould.png",
                "page": 3,
            },
            {
                "type": "caption",
                "id": "caption-1",
                "text": "Figure 1",
                "parent_block_id": "image-1",
                "page": 3,
            },
            {"type": "footnote", "text": "Test note", "page": 3},
            {"type": "header", "text": "Casting handbook", "page": 3},
            {"type": "footer", "text": "Page 3", "page": 3},
            {"type": "custom_type", "text": "Unknown block", "page": 4},
        ),
        result_files=(
            MinerUResultFile(
                file_type="markdown",
                filename="output.md",
                content_type="text/markdown",
            ),
            MinerUResultFile(
                file_type="json",
                filename="output.json",
                content_type="application/json",
            ),
        ),
        assets=(
            MinerUAssetResult(
                asset_type="image",
                asset_key="images/mould.png",
                filename="mould.png",
                mime_type="image/png",
                page_number=3,
                caption="Mould layout",
                metadata={"block_key": "image-1", "width": 800, "height": 600},
            ),
        ),
        raw_metadata={
            "task_id": "task-1",
            "api_version": "v1",
            "ignored_large_payload": {"raw": "do-not-copy"},
        },
        page_count=4,
    )


def test_normalizer_maps_supported_block_types_and_preserves_structure() -> None:
    normalized = normalize_mineru_result(
        _parse_result(),
        document_id=DOCUMENT_ID,
        parse_run_id=PARSE_RUN_ID,
    )

    assert [block.block_index for block in normalized.blocks] == list(range(11))
    assert [block.block_type for block in normalized.blocks] == [
        "title",
        "text",
        "list",
        "table",
        "formula",
        "image",
        "caption",
        "footnote",
        "header",
        "footer",
        "unknown",
    ]
    assert normalized.blocks[0].block_key == "title-1"
    assert normalized.blocks[0].bbox == [10, 20, 300, 60]
    assert normalized.blocks[0].page_start == 1
    assert normalized.blocks[1].section_path == ["Casting"]
    assert normalized.blocks[3].markdown == "|A|B|\n|-|-|\n|1|2|"
    assert normalized.blocks[3].html == "<table><tr><td>1</td></tr></table>"
    assert normalized.blocks[4].latex == "Q = mc\\Delta T"
    assert normalized.blocks[5].caption == "Mould layout"
    assert normalized.blocks[5].asset_keys == [
        (
            "parsed-assets/11111111-1111-1111-1111-111111111111/"
            "22222222-2222-2222-2222-222222222222/images/mould.png"
        )
    ]
    assert normalized.blocks[5].source_metadata["asset_keys"] == (
        normalized.blocks[5].asset_keys
    )
    assert normalized.blocks[6].parent_block_key == "image-1"


def test_normalizer_builds_stable_asset_and_raw_output_key_candidates() -> None:
    normalized = normalize_mineru_result(
        _parse_result(),
        document_id=DOCUMENT_ID,
        parse_run_id=PARSE_RUN_ID,
    )
    prefix = (
        "parsed-assets/11111111-1111-1111-1111-111111111111/"
        "22222222-2222-2222-2222-222222222222"
    )

    assert normalized.output_markdown_key == f"{prefix}/output.md"
    assert normalized.output_json_key == f"{prefix}/output.json"
    assert normalized.page_count == 4
    assert normalized.block_count == 11
    assert normalized.asset_count == 3
    assert [asset.asset_type for asset in normalized.assets] == [
        "image",
        "markdown",
        "json",
    ]
    assert normalized.assets[0].asset_key == f"{prefix}/images/mould.png"
    assert normalized.assets[0].source_block_key == "image-1"
    assert normalized.assets[1].asset_key == f"{prefix}/output.md"
    assert normalized.assets[2].asset_key == f"{prefix}/output.json"


def test_source_metadata_only_contains_a_small_allowlisted_summary() -> None:
    normalized = normalize_mineru_result(
        _parse_result(),
        document_id=DOCUMENT_ID,
        parse_run_id=PARSE_RUN_ID,
    )

    assert normalized.source_metadata == {
        "parser_name": "mineru_api",
        "parser_version": "test-v1",
        "parse_mode": "auto",
        "task_id": "task-1",
        "api_version": "v1",
    }
    assert "ignored_large_payload" not in normalized.blocks[0].source_metadata
    assert "do-not-copy" not in repr(normalized)


def test_normalizer_error_does_not_include_raw_payload_or_api_key() -> None:
    api_key = "mineru-super-secret"
    invalid = MinerUParseResult(
        parser_name="mineru_api",
        parser_version=None,
        parse_mode="auto",
        markdown_text="",
        text="",
        content_list=(api_key,),  # type: ignore[arg-type]
        raw_metadata={},
    )

    with pytest.raises(MinerUNormalizationError) as exc_info:
        normalize_mineru_result(
            invalid,
            document_id=DOCUMENT_ID,
            parse_run_id=PARSE_RUN_ID,
        )

    assert api_key not in str(exc_info.value)
    assert "index 0" in str(exc_info.value)


def test_asset_paths_cannot_escape_the_parse_run_prefix() -> None:
    result = MinerUParseResult(
        parser_name="mineru_api",
        parser_version=None,
        parse_mode="auto",
        markdown_text="",
        text="",
        assets=(
            MinerUAssetResult(
                asset_type="image",
                asset_key="../../outside.png",
                filename="outside.png",
            ),
        ),
        raw_metadata={},
    )

    normalized = normalize_mineru_result(
        result,
        document_id=DOCUMENT_ID,
        parse_run_id=PARSE_RUN_ID,
    )

    assert normalized.assets[0].asset_key.endswith("/outside.png")
    assert ".." not in normalized.assets[0].asset_key


def test_normalizer_maps_official_stable_content_list_fields() -> None:
    table_body = "<table><tr><td>A</td><td>B</td></tr></table>"
    formula = "$$Q = mc\\Delta T$$"
    result = MinerUParseResult(
        parser_name="mineru_api",
        parser_version="3.0.0",
        parse_mode="vlm",
        markdown_text="# Casting",
        text="",
        content_list=(
            {
                "type": "text",
                "text": "Casting process",
                "text_level": 1,
                "page_idx": 0,
                "bbox": [10, 20, 900, 80],
            },
            {
                "type": "text",
                "text": "Prepare the mould.",
                "page_idx": 0,
            },
            {
                "type": "list",
                "list_items": ["Dry the mould", "Inspect the cavity"],
                "page_idx": 0,
            },
            {
                "type": "table",
                "table_body": table_body,
                "table_caption": ["Table 1"],
                "table_footnote": ["Measured values"],
                "img_path": "images/table.png",
                "page_idx": 1,
            },
            {
                "type": "equation",
                "text": formula,
                "text_format": "latex",
                "img_path": "images/formula.png",
                "page_idx": 1,
            },
            {
                "type": "image",
                "img_path": "images/mould.png",
                "image_caption": ["Mould layout"],
                "image_footnote": ["Not to scale"],
                "page_idx": 2,
            },
            {
                "type": "chart",
                "img_path": "images/chart.png",
                "content": "|x|y|\n|-|-|\n|1|2|",
                "chart_caption": ["Cooling curve"],
                "chart_footnote": ["Test data"],
                "page_idx": 2,
            },
            {
                "type": "code",
                "sub_type": "algorithm",
                "code_body": "pour();\nwait();",
                "code_caption": ["Algorithm 1"],
                "page_idx": 2,
            },
            {"type": "header", "text": "Handbook", "page_idx": 2},
            {"type": "footer", "text": "Confidential", "page_idx": 2},
            {"type": "page_number", "text": "3", "page_idx": 2},
            {"type": "aside_text", "text": "Margin note", "page_idx": 2},
            {
                "type": "page_footnote",
                "text": "Page note",
                "page_idx": 2,
            },
            {"type": "future_type", "text": "Future content", "page_idx": 3},
        ),
        raw_metadata={
            "batch_id": "batch-1",
            "api_version": "v4",
            "final_status": "done",
            "file_count": 7,
            "asset_count": 4,
            "page_count": 4,
            "zip_file_types": ["content_list", "image", "markdown"],
        },
        page_count=4,
    )

    normalized = normalize_mineru_result(
        result,
        document_id=DOCUMENT_ID,
        parse_run_id=PARSE_RUN_ID,
    )

    assert [block.block_type for block in normalized.blocks] == [
        "title",
        "text",
        "list",
        "table",
        "formula",
        "image",
        "image",
        "text",
        "header",
        "footer",
        "footer",
        "footnote",
        "footnote",
        "unknown",
    ]
    assert normalized.blocks[0].text == "Casting process"
    assert normalized.blocks[0].section_path == ["Casting process"]
    assert normalized.blocks[1].section_path == ["Casting process"]
    assert normalized.blocks[0].bbox == [10, 20, 900, 80]
    assert normalized.blocks[0].page_start == 0
    assert normalized.blocks[2].text == "Dry the mould\nInspect the cavity"
    assert normalized.blocks[3].html == table_body
    assert normalized.blocks[3].caption == "Table 1\nMeasured values"
    assert normalized.blocks[4].latex == formula
    assert normalized.blocks[4].text is None
    assert normalized.blocks[5].caption == "Mould layout\nNot to scale"
    assert normalized.blocks[6].text == "|x|y|\n|-|-|\n|1|2|"
    assert normalized.blocks[6].caption == "Cooling curve\nTest data"
    assert normalized.blocks[7].text == "pour();\nwait();"
    assert normalized.blocks[7].caption == "Algorithm 1"
    assert normalized.blocks[3].asset_keys[0].endswith("/images/table.png")
    assert normalized.blocks[4].asset_keys[0].endswith("/images/formula.png")
    assert normalized.blocks[5].asset_keys[0].endswith("/images/mould.png")
    assert normalized.blocks[6].asset_keys[0].endswith("/images/chart.png")
    assert normalized.source_metadata == {
        "parser_name": "mineru_api",
        "parser_version": "3.0.0",
        "parse_mode": "vlm",
        "batch_id": "batch-1",
        "api_version": "v4",
        "final_status": "done",
        "file_count": 7,
        "asset_count": 4,
        "page_count": 4,
        "zip_file_types": ["content_list", "image", "markdown"],
    }
