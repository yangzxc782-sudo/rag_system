from __future__ import annotations

from typing import Any

from app.core.errors import DOCUMENT_DELETION_STATE_INCONSISTENT, BusinessError


ACTIVE_JOB_STATUSES = frozenset({"pending", "processing", "retry_wait"})


def validate_document_job_invariant(document: Any | None, job: Any | None) -> None:
    """Validate the approved Document/job matrix without repairing either row."""

    if document is None:
        return

    document_status = str(getattr(document, "deletion_status", ""))
    job_status = None if job is None else str(getattr(job, "status", ""))
    valid = (
        (document_status == "normal" and job is None)
        or (document_status == "deleting" and job_status in ACTIVE_JOB_STATUSES)
        or (document_status == "delete_failed" and job_status == "delete_failed")
    )
    if valid:
        return

    raise BusinessError(
        DOCUMENT_DELETION_STATE_INCONSISTENT,
        "Document deletion state is inconsistent.",
        detail={"document_status": document_status, "job_status": job_status},
        status_code=409,
    )

