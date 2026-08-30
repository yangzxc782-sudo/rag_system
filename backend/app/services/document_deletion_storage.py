from __future__ import annotations

from collections.abc import Callable


DOCUMENT_DELETION_LEASE_LOST = "DOCUMENT_DELETION_LEASE_LOST"
DOCUMENT_DELETION_MINIO_BUCKET_NOT_FOUND = (
    "DOCUMENT_DELETION_MINIO_BUCKET_NOT_FOUND"
)
DOCUMENT_DELETION_MINIO_UNAVAILABLE = "DOCUMENT_DELETION_MINIO_UNAVAILABLE"
DOCUMENT_DELETION_MINIO_DELETE_FAILED = "DOCUMENT_DELETION_MINIO_DELETE_FAILED"
DOCUMENT_DELETION_OPENSEARCH_UNAVAILABLE = (
    "DOCUMENT_DELETION_OPENSEARCH_UNAVAILABLE"
)
DOCUMENT_DELETION_OPENSEARCH_DELETE_FAILED = (
    "DOCUMENT_DELETION_OPENSEARCH_DELETE_FAILED"
)
DOCUMENT_DELETION_OPENSEARCH_INCOMPLETE = (
    "DOCUMENT_DELETION_OPENSEARCH_INCOMPLETE"
)

StorageCheckpoint = Callable[[], bool | None]


class DocumentDeletionStorageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DocumentDeletionLeaseLost(DocumentDeletionStorageError):
    def __init__(self) -> None:
        super().__init__(
            DOCUMENT_DELETION_LEASE_LOST,
            "Document deletion lease ownership was lost.",
        )


def run_storage_checkpoint(checkpoint: StorageCheckpoint) -> None:
    if checkpoint() is False:
        raise DocumentDeletionLeaseLost()
