"""Frozen block-to-chunk links are pure ordered references, without ORM access."""
import json

from app.ingestion.block_chunker import BlockChunkerConfig
from test_block_chunker import build


def test_chunk_block_links_use_stable_zero_based_order():
    result, source = build([
        dict(block_type="title", text="Casting"),
        dict(text="Prepare."),
        dict(text="Inspect."),
    ])
    ids = [b["block_id"] for b in json.loads(source.block_map)["blocks"]]
    assert len(result.chunks) == 1
    assert [(link.chunk_index, link.block_id, link.block_key, link.block_order) for link in result.links] == [
        (0, ids[0], "b0", 0), (0, ids[1], "b1", 1), (0, ids[2], "b2", 2),
    ]
    assert all(link.chunk_temp_key == "chunk-000000" for link in result.links)


def test_one_block_can_be_linked_to_multiple_split_chunks():
    result, source = build([dict(text=(
        "First paragraph contains casting preparation details."
        "\n\nSecond paragraph contains pouring details."
        "\n\nThird paragraph contains inspection details."
    ))], BlockChunkerConfig(max_chunk_chars=65, min_chunk_chars=20))
    block_id = json.loads(source.block_map)["blocks"][0]["block_id"]
    assert len(result.chunks) == len(result.links) == 3
    assert {link.chunk_index for link in result.links} == {0, 1, 2}
    assert {link.block_id for link in result.links} == {block_id}
    assert {link.block_order for link in result.links} == {0}


def test_mapping_candidates_do_not_require_database_or_chunk_orm_objects():
    result, _ = build([dict(text="Prepare.")])
    assert result.chunks[0].embedding is None
    assert result.links[0].block_key == "b0"
    assert result.links[0].chunk_temp_key == "chunk-000000"
