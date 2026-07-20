from __future__ import annotations

from app.ingestion.block_chunker import (
    BlockChunkerConfig,
    build_block_aware_chunks,
)
from app.ingestion.mineru.normalizer import NormalizedDocumentBlock

PARSE_RUN_ID = "22222222-2222-2222-2222-222222222222"


def _block(
    index: int,
    text: str,
    *,
    block_type: str = "text",
    block_id: str | None = None,
) -> NormalizedDocumentBlock:
    metadata = {"mineru_block_id": f"mineru-{index}"}
    if block_id is not None:
        metadata["document_block_id"] = block_id
    return NormalizedDocumentBlock(
        block_index=index,
        block_key=f"block-{index}",
        block_type=block_type,
        text=text,
        section_path=["Casting"],
        source_metadata=metadata,
    )


def test_chunk_block_links_use_stable_zero_based_order() -> None:
    blocks = [
        _block(0, "Casting", block_type="title", block_id="db-block-0"),
        _block(1, "Prepare.", block_id="db-block-1"),
        _block(2, "Inspect.", block_id="db-block-2"),
    ]

    result = build_block_aware_chunks(
        blocks,
        parse_run_id=PARSE_RUN_ID,
        config=BlockChunkerConfig(max_chunk_chars=500),
    )

    assert len(result.chunks) == 1
    assert [
        (link.chunk_index, link.block_id, link.block_key, link.block_order)
        for link in result.links
    ] == [
        (0, "db-block-0", "block-0", 0),
        (0, "db-block-1", "block-1", 1),
        (0, "db-block-2", "block-2", 2),
    ]
    assert all(link.chunk_temp_key == "chunk-000000" for link in result.links)


def test_one_block_can_be_linked_to_multiple_split_chunks() -> None:
    block = _block(
        0,
        (
            "First paragraph contains casting preparation details."
            "\n\nSecond paragraph contains pouring details."
            "\n\nThird paragraph contains inspection details."
        ),
        block_id="db-block-0",
    )

    result = build_block_aware_chunks(
        [block],
        parse_run_id=PARSE_RUN_ID,
        config=BlockChunkerConfig(
            max_chunk_chars=65,
            min_chunk_chars=20,
            overlap_chars=0,
        ),
    )

    assert len(result.chunks) == 3
    assert len(result.links) == 3
    assert {link.chunk_index for link in result.links} == {0, 1, 2}
    assert {link.block_id for link in result.links} == {"db-block-0"}
    assert {link.block_order for link in result.links} == {0}


def test_mapping_candidates_do_not_require_database_or_chunk_orm_objects() -> None:
    result = build_block_aware_chunks(
        [_block(0, "Prepare.")],
        parse_run_id=PARSE_RUN_ID,
    )

    assert result.chunks[0].embedding is None
    assert result.links[0].block_key == "block-0"
    assert result.links[0].chunk_temp_key == "chunk-000000"
