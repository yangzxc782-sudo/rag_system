from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.services.document_qa_evidence_deletion import DocumentQaEvidenceDeletionService


class RecordingSession:
    def __init__(self, rows, *, fail=False):
        self.rows, self.fail, self.statements = rows, fail, []
        self.flushes = 0

    def record(self, statement):
        self.statements.append(str(statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
        )))

    def scalars(self, statement):
        self.record(statement)
        return SimpleNamespace(all=lambda: self.rows)

    def execute(self, statement):
        self.record(statement)
        if self.fail:
            raise RuntimeError("source update failed")

    def flush(self):
        self.flushes += 1


def test_cleanup_targets_exact_source_and_never_acquires_qa_thread_locks():
    document_id = uuid4()
    row = SimpleNamespace(id=uuid4(), status="available", payload={"text": "evidence"})
    db = RecordingSession([row])
    result = DocumentQaEvidenceDeletionService(db).redact(document_id)
    assert result.snapshot_ids == (row.id,)
    assert row.payload is None and row.status == "source_deleted"
    assert row.redacted_document_id == document_id
    assert "ORDER BY qa_evidence_snapshots.id FOR UPDATE" in db.statements[0]
    assert all(str(document_id) in sql for sql in db.statements)
    assert all(name not in " ".join(db.statements) for name in ("qa_sessions", "qa_turns", "qa_messages"))
    assert db.flushes == 1


def test_repeat_redaction_preserves_original_audit():
    original_document, timestamp = uuid4(), datetime.now(timezone.utc)
    row = SimpleNamespace(id=uuid4(), status="source_deleted", payload=None,
                          redacted_document_id=original_document, redacted_at=timestamp)
    DocumentQaEvidenceDeletionService(RecordingSession([row])).redact(uuid4())
    assert row.redacted_document_id == original_document and row.redacted_at == timestamp


def test_cleanup_propagates_failure_to_transaction_owner():
    with pytest.raises(RuntimeError, match="source update failed"):
        DocumentQaEvidenceDeletionService(RecordingSession([], fail=True)).redact(uuid4())
