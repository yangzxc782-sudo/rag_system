from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from app.core.errors import (
    DOCUMENT_DELETION_IN_PROGRESS,
    DOCUMENT_DELETE_FAILED,
    DOCUMENT_NOT_FOUND,
    BusinessError,
)
from app.services.document_operation_guard import DocumentOperationGuard


DOCUMENT_ID = UUID("11111111-1111-1111-1111-111111111111")


class FakeSession:
    def __init__(self, document: object | None) -> None:
        self.document = document
        self.statements: list[object] = []

    def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.document


def test_guard_locks_and_accepts_normal_document() -> None:
    document = SimpleNamespace(id=DOCUMENT_ID, deletion_status="normal")
    db = FakeSession(document)

    assert DocumentOperationGuard(db).lock_normal(DOCUMENT_ID) is document

    sql = str(
        db.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "FOR UPDATE" in sql
    assert "DOCUMENTS.ID" in sql


@pytest.mark.parametrize(
    ("deletion_status", "error_code"),
    [
        ("deleting", DOCUMENT_DELETION_IN_PROGRESS),
        ("delete_failed", DOCUMENT_DELETE_FAILED),
    ],
)
def test_guard_rejects_non_normal_document(
    deletion_status: str,
    error_code: str,
) -> None:
    db = FakeSession(
        SimpleNamespace(id=DOCUMENT_ID, deletion_status=deletion_status)
    )

    with pytest.raises(BusinessError) as exc_info:
        DocumentOperationGuard(db).lock_normal(DOCUMENT_ID)

    assert exc_info.value.code == error_code
    assert exc_info.value.status_code == 409


def test_guard_reports_missing_document() -> None:
    with pytest.raises(BusinessError) as exc_info:
        DocumentOperationGuard(FakeSession(None)).lock_normal(DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_NOT_FOUND


def test_guard_optional_lock_skips_non_normal_without_error() -> None:
    db = FakeSession(
        SimpleNamespace(id=DOCUMENT_ID, deletion_status="deleting")
    )

    assert DocumentOperationGuard(db).lock_if_normal(DOCUMENT_ID) is None

