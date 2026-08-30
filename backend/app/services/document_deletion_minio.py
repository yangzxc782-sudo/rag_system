from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from minio import Minio
from minio.error import S3Error

from app.services.document_deletion_manifest import DocumentDeletionManifest
from app.services.document_deletion_storage import (
    DOCUMENT_DELETION_MINIO_BUCKET_NOT_FOUND,
    DOCUMENT_DELETION_MINIO_DELETE_FAILED,
    DOCUMENT_DELETION_MINIO_UNAVAILABLE,
    DocumentDeletionStorageError,
    StorageCheckpoint,
    run_storage_checkpoint,
)
from app.services.object_storage import (
    MinioObjectVersion,
    list_minio_object_versions,
    remove_minio_object_versions,
)


DEFAULT_HEARTBEAT_OBJECT_INTERVAL = 25
MAX_DELETE_BATCH_SIZE = 1000
_MISSING_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NoSuchVersion"})


def delete_minio_targets(
    manifest: DocumentDeletionManifest,
    *,
    client: Minio,
    checkpoint: StorageCheckpoint,
    heartbeat_interval_seconds: float,
    heartbeat_object_interval: int = DEFAULT_HEARTBEAT_OBJECT_INTERVAL,
    delete_batch_size: int = MAX_DELETE_BATCH_SIZE,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    delete_minio_derived_targets(
        manifest,
        client=client,
        checkpoint=checkpoint,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        heartbeat_object_interval=heartbeat_object_interval,
        delete_batch_size=delete_batch_size,
        monotonic=monotonic,
    )
    delete_minio_raw_target(
        manifest,
        client=client,
        checkpoint=checkpoint,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        heartbeat_object_interval=heartbeat_object_interval,
        delete_batch_size=delete_batch_size,
        monotonic=monotonic,
    )


def delete_minio_derived_targets(
    manifest: DocumentDeletionManifest,
    *,
    client: Minio,
    checkpoint: StorageCheckpoint,
    heartbeat_interval_seconds: float,
    heartbeat_object_interval: int = DEFAULT_HEARTBEAT_OBJECT_INTERVAL,
    delete_batch_size: int = MAX_DELETE_BATCH_SIZE,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    _validate_loop_settings(
        heartbeat_interval_seconds,
        heartbeat_object_interval,
        delete_batch_size,
    )
    exact_keys = tuple(manifest.derived_object_keys)
    prefixes = tuple(manifest.derived_prefixes)
    versions = _collect_target_versions(
        client=client,
        bucket_name=manifest.bucket_name,
        exact_keys=exact_keys,
        prefixes=prefixes,
        checkpoint=checkpoint,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        heartbeat_object_interval=heartbeat_object_interval,
        monotonic=monotonic,
    )
    _remove_versions(
        client=client,
        bucket_name=manifest.bucket_name,
        versions=versions,
        checkpoint=checkpoint,
        delete_batch_size=delete_batch_size,
    )
    _verify_targets_absent(
        client=client,
        bucket_name=manifest.bucket_name,
        exact_keys=exact_keys,
        prefixes=prefixes,
        checkpoint=checkpoint,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        heartbeat_object_interval=heartbeat_object_interval,
        monotonic=monotonic,
    )


def delete_minio_raw_target(
    manifest: DocumentDeletionManifest,
    *,
    client: Minio,
    checkpoint: StorageCheckpoint,
    heartbeat_interval_seconds: float,
    heartbeat_object_interval: int = DEFAULT_HEARTBEAT_OBJECT_INTERVAL,
    delete_batch_size: int = MAX_DELETE_BATCH_SIZE,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    _validate_loop_settings(
        heartbeat_interval_seconds,
        heartbeat_object_interval,
        delete_batch_size,
    )
    exact_keys = (manifest.raw_object_key,)
    versions = _collect_target_versions(
        client=client,
        bucket_name=manifest.bucket_name,
        exact_keys=exact_keys,
        prefixes=(),
        checkpoint=checkpoint,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        heartbeat_object_interval=heartbeat_object_interval,
        monotonic=monotonic,
    )
    _remove_versions(
        client=client,
        bucket_name=manifest.bucket_name,
        versions=versions,
        checkpoint=checkpoint,
        delete_batch_size=delete_batch_size,
    )
    _verify_targets_absent(
        client=client,
        bucket_name=manifest.bucket_name,
        exact_keys=exact_keys,
        prefixes=(),
        checkpoint=checkpoint,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        heartbeat_object_interval=heartbeat_object_interval,
        monotonic=monotonic,
    )


def _collect_target_versions(
    *,
    client: Minio,
    bucket_name: str,
    exact_keys: Iterable[str],
    prefixes: Iterable[str],
    checkpoint: StorageCheckpoint,
    heartbeat_interval_seconds: float,
    heartbeat_object_interval: int,
    monotonic: Callable[[], float],
) -> tuple[MinioObjectVersion, ...]:
    collected: dict[tuple[str, str | None], MinioObjectVersion] = {}
    for exact_key in exact_keys:
        for version in _list_versions(
            client=client,
            bucket_name=bucket_name,
            prefix=exact_key,
            exact_key=exact_key,
            checkpoint=checkpoint,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            heartbeat_object_interval=heartbeat_object_interval,
            monotonic=monotonic,
        ):
            collected[(version.object_key, version.version_id)] = version
    for prefix in prefixes:
        for version in _list_versions(
            client=client,
            bucket_name=bucket_name,
            prefix=prefix,
            exact_key=None,
            checkpoint=checkpoint,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            heartbeat_object_interval=heartbeat_object_interval,
            monotonic=monotonic,
        ):
            collected[(version.object_key, version.version_id)] = version
    return tuple(
        collected[key]
        for key in sorted(collected, key=lambda item: (item[0], item[1] or ""))
    )


def _list_versions(
    *,
    client: Minio,
    bucket_name: str,
    prefix: str,
    exact_key: str | None,
    checkpoint: StorageCheckpoint,
    heartbeat_interval_seconds: float,
    heartbeat_object_interval: int,
    monotonic: Callable[[], float],
) -> tuple[MinioObjectVersion, ...]:
    run_storage_checkpoint(checkpoint)
    last_checkpoint_at = monotonic()
    versions: list[MinioObjectVersion] = []
    objects_since_checkpoint = 0
    try:
        for version in list_minio_object_versions(
            client=client,
            bucket_name=bucket_name,
            prefix=prefix,
        ):
            objects_since_checkpoint += 1
            if exact_key is None or version.object_key == exact_key:
                versions.append(version)
            now = monotonic()
            if (
                objects_since_checkpoint >= heartbeat_object_interval
                or now - last_checkpoint_at >= heartbeat_interval_seconds
            ):
                run_storage_checkpoint(checkpoint)
                objects_since_checkpoint = 0
                last_checkpoint_at = now
    except S3Error as exc:
        if exc.code in _MISSING_CODES:
            return ()
        _raise_minio_error(exc)
    except DocumentDeletionStorageError:
        raise
    except Exception:
        raise DocumentDeletionStorageError(
            DOCUMENT_DELETION_MINIO_UNAVAILABLE,
            "MinIO is unavailable during document deletion.",
        ) from None
    run_storage_checkpoint(checkpoint)
    return tuple(versions)


def _remove_versions(
    *,
    client: Minio,
    bucket_name: str,
    versions: tuple[MinioObjectVersion, ...],
    checkpoint: StorageCheckpoint,
    delete_batch_size: int,
) -> None:
    for start in range(0, len(versions), delete_batch_size):
        batch = versions[start : start + delete_batch_size]
        run_storage_checkpoint(checkpoint)
        try:
            errors = list(
                remove_minio_object_versions(
                    client=client,
                    bucket_name=bucket_name,
                    versions=batch,
                )
            )
        except S3Error as exc:
            if exc.code not in _MISSING_CODES:
                _raise_minio_error(exc)
            errors = []
        except Exception:
            raise DocumentDeletionStorageError(
                DOCUMENT_DELETION_MINIO_UNAVAILABLE,
                "MinIO is unavailable during document deletion.",
            ) from None
        run_storage_checkpoint(checkpoint)
        actionable = [
            error for error in errors if getattr(error, "code", None) not in _MISSING_CODES
        ]
        if actionable:
            if any(getattr(error, "code", None) == "NoSuchBucket" for error in actionable):
                raise DocumentDeletionStorageError(
                    DOCUMENT_DELETION_MINIO_BUCKET_NOT_FOUND,
                    "MinIO bucket is unavailable during document deletion.",
                )
            raise DocumentDeletionStorageError(
                DOCUMENT_DELETION_MINIO_DELETE_FAILED,
                "MinIO object deletion did not complete.",
            )


def _verify_targets_absent(
    *,
    client: Minio,
    bucket_name: str,
    exact_keys: Iterable[str],
    prefixes: Iterable[str],
    checkpoint: StorageCheckpoint,
    heartbeat_interval_seconds: float,
    heartbeat_object_interval: int,
    monotonic: Callable[[], float],
) -> None:
    remaining = _collect_target_versions(
        client=client,
        bucket_name=bucket_name,
        exact_keys=exact_keys,
        prefixes=prefixes,
        checkpoint=checkpoint,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        heartbeat_object_interval=heartbeat_object_interval,
        monotonic=monotonic,
    )
    if remaining:
        raise DocumentDeletionStorageError(
            DOCUMENT_DELETION_MINIO_DELETE_FAILED,
            "MinIO target verification found remaining object versions.",
        )


def _raise_minio_error(exc: S3Error) -> None:
    if exc.code == "NoSuchBucket":
        raise DocumentDeletionStorageError(
            DOCUMENT_DELETION_MINIO_BUCKET_NOT_FOUND,
            "MinIO bucket is unavailable during document deletion.",
        ) from None
    raise DocumentDeletionStorageError(
        DOCUMENT_DELETION_MINIO_UNAVAILABLE,
        "MinIO is unavailable during document deletion.",
    ) from None


def _validate_loop_settings(
    heartbeat_interval_seconds: float,
    heartbeat_object_interval: int,
    delete_batch_size: int,
) -> None:
    if heartbeat_interval_seconds <= 0:
        raise ValueError("heartbeat_interval_seconds must be positive")
    if heartbeat_object_interval <= 0:
        raise ValueError("heartbeat_object_interval must be positive")
    if delete_batch_size <= 0 or delete_batch_size > MAX_DELETE_BATCH_SIZE:
        raise ValueError("delete_batch_size must be between 1 and 1000")
