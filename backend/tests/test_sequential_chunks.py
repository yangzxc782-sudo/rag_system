"""Single-interval contracts: no external services or rewritten source text."""
from copy import deepcopy
from uuid import uuid4
import random

import pytest
from app.ingestion.sequential_chunker import SegmentationConfig, split_sequential, overlaps
from test_kg_v2_protocol_units import anchor


CONFIGS = [SegmentationConfig(chunk_size=13, overlap=0, boundary="characters"),
           SegmentationConfig(chunk_size=24, overlap=7, boundary="line"),
           SegmentationConfig(chunk_size=39, overlap=9, boundary="paragraph")]


def setup(text):
    owner = dict(document_id=uuid4(), source_version=uuid4(), graph_build_id=uuid4())
    refs = tuple({**owner, "source_start": s, "source_end": e, "anchor_metadata": anchor(kind)}
                 for s, e, kind in [(2, 17, "clause"), (22, len(text), "table")])
    return owner, refs


@pytest.mark.parametrize("config", CONFIGS)
def test_three_configs_exact_substrings_and_bidirectional_mapping(config):
    text = "# 标题\n条款：σ≥350 MPa😀\n\n<table><tr><td>牌号</td><td>Al</td></tr></table>\n\n末尾"
    owner, refs = setup(text)
    chunks = split_sequential(text, config, **owner, anchors=refs, blocks=[])
    covered = set()
    for c in chunks:
        assert c.content == text[c.source_start:c.source_end]
        covered.update(range(c.source_start, c.source_end))
        expected = [r["anchor_metadata"] for r in refs if set(range(c.source_start, c.source_end)) & set(range(r["source_start"], r["source_end"]))]
        assert list(c.kg_refs) == expected
        assert not any(k.endswith("spans") for k in c.__dict__)
    assert covered == set(range(len(text)))
    assert sum(refs[1]["anchor_metadata"] in c.kg_refs for c in chunks) > 1
    assert chunks[0].source_start == 0 and chunks[-1].source_end == len(text)


def test_endpoints_empty_duplicate_conflict_and_wrong_owner():
    text = "a" * 60
    owner, refs = setup(text)
    assert not overlaps(0, 2, 2, 17)
    assert not overlaps(17, 22, 2, 17)
    config = SegmentationConfig(chunk_size=60, overlap=0)
    chunks = split_sequential(text, config, **owner, anchors=refs + refs, blocks=[])
    assert len(chunks[0].kg_refs) == 2
    bad = deepcopy(refs[0]); bad["source_end"] += 1
    with pytest.raises(ValueError, match="Conflicting"):
        split_sequential(text, config, **owner, anchors=refs + (bad,), blocks=[])
    bad = deepcopy(refs[0]); bad["source_version"] = uuid4()
    with pytest.raises(ValueError, match="another"):
        split_sequential(text, config, **owner, anchors=(bad,), blocks=[])
    for content in ("", "正文<!-- kg-anchor-start\nanchor_id: x\n-->内容"):
        with pytest.raises(ValueError):
            split_sequential(content, config, **owner, anchors=(), blocks=[])


def test_seeded_interval_intersections_and_progress():
    rng = random.Random(13)
    text = "同标题😀\n\n重复正文\n" * 18
    owner, refs = setup(text)
    for _ in range(80):
        size = rng.randrange(1, 80)
        config = SegmentationConfig(chunk_size=size, overlap=rng.randrange(size), boundary=rng.choice(["line", "paragraph", "characters"]))
        chunks = split_sequential(text, config, **owner, anchors=refs, blocks=[])
        assert all(a.source_start < b.source_start <= a.source_end for a, b in zip(chunks, chunks[1:]))
        assert all(c.content == text[c.source_start:c.source_end] for c in chunks)
        assert chunks[-1].source_end == len(text)


def test_cross_page_and_block_are_auxiliary_not_chunk_boundaries():
    owner, refs = setup("x" * 40)
    blocks = [dict(block_id="a", source_start=0, source_end=20, page_start=1, page_end=1),
              dict(block_id="b", source_start=20, source_end=40, page_start=2, page_end=2)]
    c, = split_sequential("x" * 40, SegmentationConfig(chunk_size=40, overlap=0), **owner, anchors=refs, blocks=blocks)
    assert c.block_ids == ("a", "b") and (c.page_start, c.page_end) == (1, 2)
