from __future__ import annotations

from uuid import UUID

from app.ingestion.mineru.normalizer import (
    NormalizedDocumentAsset,
    NormalizedDocumentBlock,
)
from app.models.document_asset import DocumentAsset
from app.models.document_block import DocumentBlock
from app.services.document_assets import (
    add_document_assets,
    build_document_assets,
)
from app.services.document_blocks import (
    add_document_blocks,
    build_document_blocks,
)

DOCUMENT_ID = UUID("11111111-1111-1111-1111-111111111111")
PARSE_RUN_ID = UUID("22222222-2222-2222-2222-222222222222")


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush_count = 0

    def add_all(self, instances: list[object]) -> None:
        self.added.extend(instances)

    def flush(self) -> None:
        self.flush_count += 1


def _blocks() -> list[NormalizedDocumentBlock]:
    return [
        NormalizedDocumentBlock(
            block_index=0,
            block_key="title-1",
            block_type="title",
            page_start=1,
            page_end=1,
            bbox=[10, 20, 300, 60],
            text="Casting",
            section_path=["Casting"],
            source_metadata={"mineru_block_id": "title-1"},
        ),
        NormalizedDocumentBlock(
            block_index=1,
            block_key="text-1",
            block_type="text",
            page_start=1,
            page_end=1,
            text="Prepare the mould.",
            parent_block_key="title-1",
            section_path=["Casting"],
            asset_keys=["parsed-assets/document/run/images/reference.png"],
            source_metadata={"mineru_block_id": "text-1"},
        ),
    ]


def _assets() -> list[NormalizedDocumentAsset]:
    return [
        NormalizedDocumentAsset(
            asset_type="image",
            page_number=1,
            asset_key=(
                "parsed-assets/11111111-1111-1111-1111-111111111111/"
                "22222222-2222-2222-2222-222222222222/images/mould.png"
            ),
            filename="mould.png",
            mime_type="image/png",
            size_bytes=128,
            caption="Mould layout",
            source_block_key="image-1",
            source_metadata={"width": 800, "height": 600},
        )
    ]


def test_build_document_blocks_constructs_orm_objects_without_database() -> None:
    objects = build_document_blocks(
        document_id=DOCUMENT_ID,
        parse_run_id=PARSE_RUN_ID,
        blocks=_blocks(),
    )

    assert all(isinstance(item, DocumentBlock) for item in objects)
    assert [item.block_index for item in objects] == [0, 1]
    assert objects[0].document_id == DOCUMENT_ID
    assert objects[0].parse_run_id == PARSE_RUN_ID
    assert objects[0].bbox == [10, 20, 300, 60]
    assert objects[1].parent_block_key == "title-1"
    assert objects[1].section_path == ["Casting"]
    assert objects[1].source_metadata["asset_keys"] == [
        "parsed-assets/document/run/images/reference.png"
    ]


def test_add_document_blocks_uses_injected_session_without_commit() -> None:
    session = FakeSession()

    objects = add_document_blocks(
        session,
        document_id=DOCUMENT_ID,
        parse_run_id=PARSE_RUN_ID,
        blocks=_blocks(),
    )

    assert session.added == objects
    assert session.flush_count == 1


def test_build_document_assets_constructs_orm_objects_without_storage() -> None:
    objects = build_document_assets(
        document_id=DOCUMENT_ID,
        parse_run_id=PARSE_RUN_ID,
        assets=_assets(),
    )

    assert all(isinstance(item, DocumentAsset) for item in objects)
    assert objects[0].document_id == DOCUMENT_ID
    assert objects[0].parse_run_id == PARSE_RUN_ID
    assert objects[0].asset_key.endswith("/images/mould.png")
    assert objects[0].source_block_key == "image-1"
    assert objects[0].source_metadata == {"width": 800, "height": 600}
    assert not hasattr(objects[0], "content")


def test_add_document_assets_uses_injected_session_without_commit() -> None:
    session = FakeSession()

    objects = add_document_assets(
        session,
        document_id=DOCUMENT_ID,
        parse_run_id=PARSE_RUN_ID,
        assets=_assets(),
    )

    assert session.added == objects
    assert session.flush_count == 1
