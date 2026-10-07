"""Plain PDF text/metadata fixtures for shared writer and retrieval contracts.

No parser entry, KG model, or sequential chunk implementation is simulated here.
"""
from copy import deepcopy

from app.ingestion.chunk_drafts import ChunkDraft


TABLE_REF = {"anchor_id": "G::T0001", "graph_id": "G", "anchor_type": "table", "table_ref": "T0001"}
CLAUSE_REF = {"anchor_id": "G::C0001", "graph_id": "G", "anchor_type": "clause", "clause_ref": "C0001"}


def pdf_draft(content="| Si | wt.% |\n| --- | --- |\n| ≥6.50 | ≤7.50 |", refs=None):
    return ChunkDraft(
        chunk_index=0, content=content, token_count=len(content), page_start=1, page_end=1,
        section_title=None, chunk_type="table", chunk_method="pdf_fixture", content_format="markdown",
        source_metadata={"parser_provider": "mineru_api", "kg_refs": deepcopy(refs if refs is not None else [CLAUSE_REF, TABLE_REF])},
    )
