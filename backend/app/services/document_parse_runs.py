from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import DOCUMENT_NOT_FOUND, BusinessError
from app.models.document import Document
from app.models.document_parse_run import DocumentParseRun


class ParseRunSessionProtocol(Protocol):
    def add(self, instance: Any) -> None:
        ...

    def scalars(self, statement: Any) -> Any:
        ...


@dataclass(frozen=True)
class DocumentParseRunSummary:
    id: UUID
    document_id: UUID
    parser_provider: str
    parser_version: str | None
    parse_mode: str | None
    status: str
    is_active: bool
    input_file_key: str | None
    output_prefix: str | None
    output_markdown_key: str | None
    output_json_key: str | None
    output_markdown_status: str
    output_json_status: str
    failure_status_persisted: bool | None
    page_count: int | None
    block_count: int | None
    asset_count: int | None
    error_message: str | None
    source_metadata_summary: dict[str, Any]
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class DocumentParseRunListResult:
    items: list[DocumentParseRunSummary]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class DocumentParseStatusResult:
    document_id: UUID
    process_status: str
    latest_parse_run: DocumentParseRunSummary | None
    active_parse_run: DocumentParseRunSummary | None


def list_parse_runs(
    db: Session,
    document_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> DocumentParseRunListResult:
    _get_document_or_raise(db, document_id)
    total = db.scalar(
        select(func.count())
        .select_from(DocumentParseRun)
        .where(DocumentParseRun.document_id == document_id)
    ) or 0
    runs = list(
        db.scalars(
            select(DocumentParseRun)
            .where(DocumentParseRun.document_id == document_id)
            .order_by(DocumentParseRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    return DocumentParseRunListResult(
        items=[_to_parse_run_summary(parse_run) for parse_run in runs],
        total=total,
        limit=limit,
        offset=offset,
    )


def get_parse_status(
    db: Session,
    document_id: UUID,
) -> DocumentParseStatusResult:
    document = _get_document_or_raise(db, document_id)
    latest = db.scalars(
        select(DocumentParseRun)
        .where(DocumentParseRun.document_id == document_id)
        .order_by(DocumentParseRun.created_at.desc())
        .limit(1)
    ).first()
    active = db.scalars(
        select(DocumentParseRun)
        .where(
            DocumentParseRun.document_id == document_id,
            DocumentParseRun.is_active.is_(True),
        )
        .order_by(DocumentParseRun.created_at.desc())
        .limit(1)
    ).first()
    return DocumentParseStatusResult(
        document_id=document_id,
        process_status=document.process_status,
        latest_parse_run=(
            _to_parse_run_summary(latest) if latest is not None else None
        ),
        active_parse_run=(
            _to_parse_run_summary(active) if active is not None else None
        ),
    )


def create_parse_run(
    db: ParseRunSessionProtocol,
    *,
    document_id: UUID,
    parser_provider: str,
    input_file_key: str,
    parse_mode: str | None,
    output_base_prefix: str,
    source_metadata: Mapping[str, Any] | None = None,
) -> DocumentParseRun:
    parse_run_id = uuid4()
    base_prefix = output_base_prefix.strip("/") or "parsed-assets"
    output_prefix = (
        f"{base_prefix}/{document_id}/{parse_run_id}"
    )
    parse_run = DocumentParseRun(
        id=parse_run_id,
        document_id=document_id,
        parser_provider=parser_provider,
        parse_mode=parse_mode,
        status="pending",
        is_active=False,
        input_file_key=input_file_key,
        output_prefix=output_prefix,
        source_metadata=dict(source_metadata or {}),
    )
    db.add(parse_run)
    return parse_run


def mark_running(
    db: ParseRunSessionProtocol,
    parse_run: DocumentParseRun,
) -> DocumentParseRun:
    parse_run.status = "running"
    parse_run.is_active = False
    parse_run.error_message = None
    parse_run.started_at = datetime.now(timezone.utc)
    parse_run.completed_at = None
    db.add(parse_run)
    return parse_run


def mark_succeeded(
    db: ParseRunSessionProtocol,
    parse_run: DocumentParseRun,
    *,
    parser_version: str | None,
    output_markdown_key: str,
    output_json_key: str,
    page_count: int | None,
    block_count: int,
    asset_count: int,
    source_metadata: Mapping[str, Any],
) -> DocumentParseRun:
    deactivate_other_runs_if_needed(
        db,
        document_id=parse_run.document_id,
        active_parse_run_id=parse_run.id,
    )
    parse_run.parser_version = parser_version
    parse_run.output_markdown_key = output_markdown_key
    parse_run.output_json_key = output_json_key
    parse_run.page_count = page_count
    parse_run.block_count = block_count
    parse_run.asset_count = asset_count
    parse_run.source_metadata = dict(source_metadata)
    parse_run.status = "succeeded"
    parse_run.is_active = True
    parse_run.error_message = None
    parse_run.completed_at = datetime.now(timezone.utc)
    db.add(parse_run)
    return parse_run


def mark_failed(
    db: ParseRunSessionProtocol,
    parse_run: DocumentParseRun,
    *,
    error_message: str,
    secrets: Iterable[str] = (),
) -> DocumentParseRun:
    parse_run.status = "failed"
    parse_run.is_active = False
    parse_run.error_message = sanitize_error_message(
        error_message,
        secrets=secrets,
    )
    parse_run.completed_at = datetime.now(timezone.utc)
    db.add(parse_run)
    return parse_run


def deactivate_other_runs_if_needed(
    db: ParseRunSessionProtocol,
    *,
    document_id: UUID,
    active_parse_run_id: UUID,
) -> None:
    other_active_runs = list(
        db.scalars(
            select(DocumentParseRun).where(
                DocumentParseRun.document_id == document_id,
                DocumentParseRun.is_active.is_(True),
                DocumentParseRun.id != active_parse_run_id,
            )
        ).all()
    )
    for parse_run in other_active_runs:
        parse_run.is_active = False
        db.add(parse_run)


def sanitize_error_message(
    message: str,
    *,
    secrets: Iterable[str] = (),
    max_length: int = 500,
) -> str:
    sanitized = message
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized[:max_length] or "文档解析失败。"


def _get_document_or_raise(db: Session, document_id: UUID) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise BusinessError(
            DOCUMENT_NOT_FOUND,
            "文档不存在。",
            detail={"document_id": str(document_id)},
            status_code=404,
        )
    return document


def _to_parse_run_summary(
    parse_run: DocumentParseRun,
) -> DocumentParseRunSummary:
    metadata = parse_run.source_metadata or {}
    failure_status = metadata.get("failure_status_persisted")
    if not isinstance(failure_status, bool):
        failure_status = True if parse_run.status == "failed" else None
    return DocumentParseRunSummary(
        id=parse_run.id,
        document_id=parse_run.document_id,
        parser_provider=parse_run.parser_provider,
        parser_version=parse_run.parser_version,
        parse_mode=parse_run.parse_mode,
        status=parse_run.status,
        is_active=parse_run.is_active,
        input_file_key=parse_run.input_file_key,
        output_prefix=parse_run.output_prefix,
        output_markdown_key=parse_run.output_markdown_key,
        output_json_key=parse_run.output_json_key,
        output_markdown_status=_output_status(
            metadata.get("output_markdown_status")
        ),
        output_json_status=_output_status(
            metadata.get("output_json_status")
        ),
        failure_status_persisted=failure_status,
        page_count=parse_run.page_count,
        block_count=parse_run.block_count,
        asset_count=parse_run.asset_count,
        error_message=parse_run.error_message,
        source_metadata_summary=_parse_run_metadata_summary(metadata),
        started_at=parse_run.started_at,
        completed_at=parse_run.completed_at,
        created_at=parse_run.created_at,
    )


def _output_status(value: Any) -> str:
    if value in {"saved", "download_deferred", "unavailable"}:
        return str(value)
    return "unavailable"


def _parse_run_metadata_summary(
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    allowed_keys = {
        "api_version",
        "deferred_file_count",
        "failure_status_persisted",
        "intermediate_file_count",
        "parse_mode",
        "task_id",
    }
    return {
        key: summary
        for key in allowed_keys
        if key in metadata
        and (summary := _summary_value(metadata[key])) is not None
    }


def _summary_value(value: Any) -> str | int | float | bool | None:
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, (int, float, bool)):
        return value
    return None
