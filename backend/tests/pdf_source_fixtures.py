"""Plain PDF text/metadata fixtures for shared writer and retrieval contracts.

No parser entry, KG model, or sequential chunk implementation is simulated here.
"""
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

from app.ingestion.chunk_drafts import ChunkDraft
from app.ingestion.frozen_source import render_frozen_source


TABLE_REF = {"anchor_id": "G::T0001", "graph_id": "G", "anchor_type": "table", "table_ref": "T0001"}
CLAUSE_REF = {"anchor_id": "G::C0001", "graph_id": "G", "anchor_type": "clause", "clause_ref": "C0001"}


def frozen_blocks(values):
    """Freeze synthetic block dictionaries through the real S1 contract, without IO."""
    identity = dict(document_id=UUID(int=1), parse_run_id=UUID(int=2), source_version=UUID(int=3))
    blocks = []
    for i, value in enumerate(deepcopy(values)):
        fields = dict(id=UUID(int=100 + i), document_id=identity["document_id"],
                      parse_run_id=identity["parse_run_id"], block_index=i, block_key=f"b{i}",
                      block_type="text", text="", page_start=0, page_end=0,
                      section_path=[], asset_keys=[], source_metadata={})
        fields.update(value)
        blocks.append(SimpleNamespace(**fields))
    assets = [key for b in blocks for key in (b.asset_keys or b.source_metadata.get("asset_keys", []))]
    return render_frozen_source(blocks, **identity, output_prefix="run", registered_asset_keys=assets), identity


def structured_source_blocks():
    """Synthetic renderer/M2 baseline; fixed IDs, Unicode, repeats and a long table."""
    identity = dict(document_id=UUID(int=1), parse_run_id=UUID(int=2), source_version=UUID(int=3))
    values = [
        dict(block_type="header", text="页眉"),
        dict(block_type="title", text="6 要求"),
        dict(block_type="text", text="e\u0301😀\r\n重复"),
        dict(block_type="table", text="", caption="表1", html="<table>" +
             "".join("<tr><td>合成</td><td>350 MPa</td></tr>" for _ in range(85)) + "</table>"),
        dict(block_type="formula", text="", latex=r"A \wedge B"),
        dict(block_type="title", text="6.1 要求"),
        dict(block_type="text", text="重复"),
    ]
    blocks = [SimpleNamespace(
        id=UUID(int=10 + i), document_id=identity["document_id"], parse_run_id=identity["parse_run_id"],
        block_index=i, block_key=f"b{i}", page_start=i // 2, page_end=i // 2,
        section_path=[], source_metadata={}, **value,
    ) for i, value in enumerate(values)]
    return blocks, identity


def pdf_draft(content="| Si | wt.% |\n| --- | --- |\n| ≥6.50 | ≤7.50 |", refs=None):
    return ChunkDraft(
        chunk_index=0, content=content, token_count=len(content), page_start=1, page_end=1,
        section_title=None, chunk_type="table", chunk_method="pdf_fixture", content_format="markdown",
        source_metadata={"parser_provider": "mineru_api", "kg_refs": deepcopy(refs if refs is not None else [CLAUSE_REF, TABLE_REF])},
    )
