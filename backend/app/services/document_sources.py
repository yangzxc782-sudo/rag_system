"""M1 frozen-source persistence helpers; no graph or chunk execution."""
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.errors import BusinessError, DOCUMENT_SOURCE_SCHEMA_UNAVAILABLE
from app.models.document_source_version import SourceDocumentVersion


def require_source_schema(db: Session) -> None:
    inspector = inspect(db.get_bind())
    table = SourceDocumentVersion.__tablename__
    if not inspector.has_table(table) or not set(SourceDocumentVersion.__table__.columns.keys()).issubset(
        column["name"] for column in inspector.get_columns(table)
    ):
        raise BusinessError(
            DOCUMENT_SOURCE_SCHEMA_UNAVAILABLE,
            "冻结来源所需数据库结构尚未就绪，请先完成已批准的迁移。",
            status_code=503,
        )
