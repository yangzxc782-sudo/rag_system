"""Exact-source redaction inside the caller's document deletion transaction."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.qa_evidence_snapshot import QAEvidenceSnapshot
from app.models.qa_evidence_source import QAEvidenceSource


@dataclass(frozen=True, slots=True)
class QaEvidenceRedaction:
    document_id: UUID
    snapshot_ids: tuple[UUID, ...]


class DocumentQaEvidenceDeletionService:
    """Never locks a thread/session, commits, or edits successful QA messages.

The finalizer already holds the Document lock. Writers take Document locks
before publishing evidence, so no new matching snapshot can escape the sweep.
An absent document is also valid during idempotent finalization recovery.
"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def redact(self, document_id: UUID) -> QaEvidenceRedaction:
        affected = select(QAEvidenceSource.snapshot_id).where(QAEvidenceSource.document_id == document_id)
        rows = self.db.scalars(select(QAEvidenceSnapshot).where(
            QAEvidenceSnapshot.id.in_(affected),
        ).order_by(QAEvidenceSnapshot.id).execution_options(populate_existing=True).with_for_update()).all()
        for row in rows:
            if row.status != "source_deleted":
                row.payload = None
                row.status = "source_deleted"
                row.redacted_document_id = document_id
                row.redacted_at = func.now()
        # Preserve first redaction timestamps and other still-valid source rows.
        self.db.execute(update(QAEvidenceSource).where(
            QAEvidenceSource.document_id == document_id,
            QAEvidenceSource.status != "source_deleted",
        ).values(status="source_deleted", deleted_at=func.now()))
        self.db.flush()
        return QaEvidenceRedaction(document_id, tuple(row.id for row in rows))
