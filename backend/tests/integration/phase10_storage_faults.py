from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from minio.deleteobjects import DeleteError, DeleteObject


class OneShotPartialDeleteMinioClient:
    """Delete a real prefix once, then inject one item-level failure."""

    def __init__(self, delegate: Any, *, successful_prefix_size: int = 1) -> None:
        if successful_prefix_size <= 0:
            raise ValueError("successful_prefix_size must be greater than zero")
        self._delegate = delegate
        self._successful_prefix_size = successful_prefix_size
        self._fault_injected = False
        self.first_attempt_deleted: tuple[tuple[str | None, str | None], ...] = ()
        self.retry_attempted: tuple[tuple[str | None, str | None], ...] = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def remove_objects(
        self,
        bucket_name: str,
        delete_objects: Iterable[DeleteObject],
    ) -> Iterator[DeleteError]:
        requested = tuple(delete_objects)
        if self._fault_injected:
            self.retry_attempted = tuple(
                (item.name, item.version_id) for item in requested
            )
            return self._delegate.remove_objects(bucket_name, requested)

        if len(requested) <= self._successful_prefix_size:
            raise AssertionError(
                "Partial deletion fixture requires an undeleted suffix."
            )
        self._fault_injected = True
        successful = requested[: self._successful_prefix_size]
        errors = tuple(self._delegate.remove_objects(bucket_name, successful))
        if errors:
            return iter(errors)
        self.first_attempt_deleted = tuple(
            (item.name, item.version_id) for item in successful
        )
        failed = requested[self._successful_prefix_size]
        return iter(
            (
                DeleteError(
                    "InternalError",
                    "controlled Phase 10 partial deletion fault",
                    failed.name,
                    failed.version_id,
                ),
            )
        )
