from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import (
    DOCUMENT_DELETION_IN_PROGRESS,
    DOCUMENT_DELETION_STATE_INCONSISTENT,
    DOCUMENT_DELETE_FAILED,
    DOCUMENT_NOT_FOUND,
    BusinessError,
)
from app.models.document import Document


class DocumentOperationGuard:
    """Lock and re-read a Document immediately before a persistent write."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def lock_normal(self, document_id: UUID) -> Document:
        document = self._lock(document_id)
        if document is None:
            raise BusinessError(
                DOCUMENT_NOT_FOUND,
                "Document not found.",
                detail={"document_id": str(document_id)},
                status_code=404,
            )
        self._require_normal(document)
        return document

    def lock_if_normal(self, document_id: UUID) -> Document | None:
        document = self._lock(document_id)
        if document is None or document.deletion_status != "normal":
            return None
        return document

    def lock_normal_many(self, document_ids: set[UUID]) -> tuple[Document, ...]:
        return tuple(
            self.lock_normal(document_id)
            for document_id in sorted(document_ids, key=str)
        )

    def _lock(self, document_id: UUID) -> Document | None:
        return self._db.scalar(
            select(Document)
            .where(Document.id == document_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )

    @staticmethod
    def _require_normal(document: Document) -> None:
        if document.deletion_status == "normal":
            return
        if document.deletion_status == "deleting":
            raise BusinessError(
                DOCUMENT_DELETION_IN_PROGRESS,
                "Document deletion is in progress.",
                detail={"document_id": str(document.id)},
                status_code=409,
            )
        if document.deletion_status == "delete_failed":
            raise BusinessError(
                DOCUMENT_DELETE_FAILED,
                "Document deletion failed and must be retried.",
                detail={"document_id": str(document.id)},
                status_code=409,
            )
        raise BusinessError(
            DOCUMENT_DELETION_STATE_INCONSISTENT,
            "Document deletion state is inconsistent.",
            detail={"document_id": str(document.id)},
            status_code=500,
        )
