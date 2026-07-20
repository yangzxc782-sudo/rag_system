from __future__ import annotations

from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import (
    DOCUMENT_NOT_FOUND,
    DOCUMENT_PARSE_RUN_NOT_FOUND,
    BusinessError,
)
from app.ingestion.mineru.normalizer import NormalizedDocumentAsset
from app.models.document import Document
from app.models.document_asset import DocumentAsset
from app.models.document_parse_run import DocumentParseRun


class DocumentAssetSessionProtocol(Protocol):
    def add_all(self, instances: Sequence[Any]) -> None:
        ...

    def flush(self) -> None:
        ...


@dataclass(frozen=True)
class DocumentAssetSummary:
    id: UUID
    document_id: UUID
    parse_run_id: UUID
    asset_type: str
    page_number: int | None
    asset_key: str
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    caption: str | None
    source_block_key: str | None
    source_metadata_summary: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class DocumentAssetListResult:
    items: list[DocumentAssetSummary]
    total: int
    limit: int
    offset: int


def list_assets(
    db: Session,
    document_id: UUID,
    *,
    parse_run_id: UUID | None = None,
    asset_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DocumentAssetListResult:
    _validate_document_and_parse_run(db, document_id, parse_run_id)
    filters = [DocumentAsset.document_id == document_id]
    if parse_run_id is not None:
        filters.append(DocumentAsset.parse_run_id == parse_run_id)
    normalized_type = asset_type.strip().lower() if asset_type else None
    if normalized_type:
        filters.append(DocumentAsset.asset_type == normalized_type)

    total = db.scalar(
        select(func.count())
        .select_from(DocumentAsset)
        .where(*filters)
    ) or 0
    assets = list(
        db.scalars(
            select(DocumentAsset)
            .where(*filters)
            .order_by(DocumentAsset.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    return DocumentAssetListResult(
        items=[_to_asset_summary(asset) for asset in assets],
        total=total,
        limit=limit,
        offset=offset,
    )


def build_document_assets(
    *,
    document_id: UUID,
    parse_run_id: UUID,
    assets: Iterable[NormalizedDocumentAsset],
) -> list[DocumentAsset]:
    candidates = list(assets)
    _validate_asset_keys(candidates)
    return [
        DocumentAsset(
            document_id=document_id,
            parse_run_id=parse_run_id,
            asset_type=asset.asset_type,
            page_number=asset.page_number,
            asset_key=asset.asset_key,
            filename=asset.filename,
            mime_type=asset.mime_type,
            size_bytes=asset.size_bytes,
            caption=asset.caption,
            source_block_key=asset.source_block_key,
            source_metadata=deepcopy(asset.source_metadata),
        )
        for asset in candidates
    ]


def add_document_assets(
    session: DocumentAssetSessionProtocol,
    *,
    document_id: UUID,
    parse_run_id: UUID,
    assets: Iterable[NormalizedDocumentAsset],
    flush: bool = True,
) -> list[DocumentAsset]:
    objects = build_document_assets(
        document_id=document_id,
        parse_run_id=parse_run_id,
        assets=assets,
    )
    if objects:
        session.add_all(objects)
        if flush:
            session.flush()
    return objects


def _validate_asset_keys(
    assets: Sequence[NormalizedDocumentAsset],
) -> None:
    asset_keys = [asset.asset_key for asset in assets]
    if len(asset_keys) != len(set(asset_keys)):
        raise ValueError("Normalized document asset keys must be unique")


def _validate_document_and_parse_run(
    db: Session,
    document_id: UUID,
    parse_run_id: UUID | None,
) -> None:
    if db.get(Document, document_id) is None:
        raise BusinessError(
            DOCUMENT_NOT_FOUND,
            "文档不存在。",
            detail={"document_id": str(document_id)},
            status_code=404,
        )
    if parse_run_id is None:
        return
    parse_run = db.get(DocumentParseRun, parse_run_id)
    if parse_run is None or parse_run.document_id != document_id:
        raise BusinessError(
            DOCUMENT_PARSE_RUN_NOT_FOUND,
            "解析任务不存在或不属于当前文档。",
            detail={
                "document_id": str(document_id),
                "parse_run_id": str(parse_run_id),
            },
            status_code=404,
        )


def _to_asset_summary(asset: DocumentAsset) -> DocumentAssetSummary:
    caption = asset.caption
    if caption is not None and len(caption) > 500:
        caption = f"{caption[:500]}..."
    return DocumentAssetSummary(
        id=asset.id,
        document_id=asset.document_id,
        parse_run_id=asset.parse_run_id,
        asset_type=asset.asset_type,
        page_number=asset.page_number,
        asset_key=asset.asset_key,
        filename=asset.filename,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        caption=caption,
        source_block_key=asset.source_block_key,
        source_metadata_summary=_asset_metadata_summary(
            deepcopy(asset.source_metadata or {})
        ),
        created_at=asset.created_at,
    )


def _asset_metadata_summary(
    metadata: dict[str, Any],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in (
        "checksum",
        "height",
        "origin",
        "result_file_type",
        "width",
    ):
        value = metadata.get(key)
        if isinstance(value, (str, int, float, bool)):
            summary[key] = value[:200] if isinstance(value, str) else value
    return summary
