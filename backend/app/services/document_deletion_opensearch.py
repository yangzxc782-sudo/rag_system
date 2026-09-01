from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.search_engine.client import SearchEngineClientProtocol
from app.services.document_deletion_manifest import DocumentDeletionManifest
from app.services.document_deletion_storage import (
    DOCUMENT_DELETION_OPENSEARCH_DELETE_FAILED,
    DOCUMENT_DELETION_OPENSEARCH_INCOMPLETE,
    DOCUMENT_DELETION_OPENSEARCH_UNAVAILABLE,
    DocumentDeletionStorageError,
    StorageCheckpoint,
    run_storage_checkpoint,
)
from app.services.search_index import build_document_delete_query


DEFAULT_STORAGE_TIMEOUT_SECONDS = 30
DEFAULT_CHUNK_VERIFICATION_BATCH_SIZE = 500
_CONCRETE_INDEX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def delete_opensearch_targets(
    manifest: DocumentDeletionManifest,
    *,
    client: SearchEngineClientProtocol,
    checkpoint: StorageCheckpoint,
    current_index_name: str,
    current_index_alias: str,
    timeout_seconds: int = DEFAULT_STORAGE_TIMEOUT_SECONDS,
    chunk_batch_size: int = DEFAULT_CHUNK_VERIFICATION_BATCH_SIZE,
) -> None:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if chunk_batch_size <= 0:
        raise ValueError("chunk_batch_size must be positive")

    targets = _resolve_concrete_targets(
        manifest,
        client=client,
        checkpoint=checkpoint,
        current_index_name=current_index_name,
        current_index_alias=current_index_alias,
    )
    for target in targets:
        response = _delete_document_from_target(
            manifest,
            client=client,
            target=target,
            checkpoint=checkpoint,
            timeout_seconds=timeout_seconds,
        )
        if response is None:
            continue
        if not isinstance(response, Mapping):
            _delete_failed()
        if bool(response.get("timed_out")) or bool(response.get("failures")):
            _delete_failed()

    verification_targets = _resolve_concrete_targets(
        manifest,
        client=client,
        checkpoint=checkpoint,
        current_index_name=current_index_name,
        current_index_alias=current_index_alias,
    )
    if any(target not in targets for target in verification_targets):
        _incomplete()
    for target in verification_targets:
        document_count = _count_target(
            client=client,
            target=target,
            body=build_document_delete_query(manifest.document_id),
            checkpoint=checkpoint,
            timeout_seconds=timeout_seconds,
        )
        if document_count != 0:
            _incomplete()

        chunk_ids = [str(value) for value in manifest.chunk_ids]
        for start in range(0, len(chunk_ids), chunk_batch_size):
            batch = chunk_ids[start : start + chunk_batch_size]
            if not batch:
                continue
            chunk_count = _count_target(
                client=client,
                target=target,
                body={
                    "query": {
                        "bool": {
                            "should": [
                                {"ids": {"values": batch}},
                                {"terms": {"chunk_id": batch}},
                            ],
                            "minimum_should_match": 1,
                        }
                    }
                },
                checkpoint=checkpoint,
                timeout_seconds=timeout_seconds,
            )
            if chunk_count != 0:
                _incomplete()


def _resolve_concrete_targets(
    manifest: DocumentDeletionManifest,
    *,
    client: SearchEngineClientProtocol,
    checkpoint: StorageCheckpoint,
    current_index_name: str,
    current_index_alias: str,
) -> tuple[str, ...]:
    targets: list[str] = []
    seen_physical_names: set[str] = set()
    seen_alias_names: set[str] = set()
    sources = (
        ("physical", manifest.search_index_name),
        ("alias", manifest.search_index_alias),
        ("physical", current_index_name),
        ("alias", current_index_alias),
    )
    for source_type, raw_name in sources:
        name = _validated_target_name(raw_name)
        if source_type == "physical":
            if name in seen_physical_names:
                continue
            seen_physical_names.add(name)
            if _index_exists(client, name, checkpoint=checkpoint):
                targets.append(name)
            continue

        if name in seen_alias_names:
            continue
        seen_alias_names.add(name)
        targets.extend(
            _resolve_alias_targets(
                client,
                name,
                checkpoint=checkpoint,
            )
        )

    return tuple(dict.fromkeys(targets))


def _resolve_alias_targets(
    client: SearchEngineClientProtocol,
    alias: str,
    *,
    checkpoint: StorageCheckpoint,
) -> tuple[str, ...]:
    if not _alias_exists(client, alias, checkpoint=checkpoint):
        return ()
    run_storage_checkpoint(checkpoint)
    try:
        response = client.indices.get_alias(name=alias)
    except Exception as exc:
        run_storage_checkpoint(checkpoint)
        if _is_not_found(exc):
            return ()
        _unavailable()
    run_storage_checkpoint(checkpoint)
    if not isinstance(response, Mapping):
        _unavailable()
    targets = tuple(sorted(str(value) for value in response))
    if any(_CONCRETE_INDEX_PATTERN.fullmatch(target) is None for target in targets):
        _unavailable()
    return targets


def _validated_target_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or _CONCRETE_INDEX_PATTERN.fullmatch(value) is None
        or value in {"_all", "*"}
        or "*" in value
        or "," in value
    ):
        _unavailable()
    return value


def _index_exists(
    client: SearchEngineClientProtocol,
    index: str,
    *,
    checkpoint: StorageCheckpoint,
) -> bool:
    run_storage_checkpoint(checkpoint)
    try:
        exists = bool(client.indices.exists(index=index))
    except Exception as exc:
        run_storage_checkpoint(checkpoint)
        if _is_not_found(exc):
            return False
        _unavailable()
    run_storage_checkpoint(checkpoint)
    return exists


def _alias_exists(
    client: SearchEngineClientProtocol,
    alias: str,
    *,
    checkpoint: StorageCheckpoint,
) -> bool:
    run_storage_checkpoint(checkpoint)
    try:
        exists = bool(client.indices.exists_alias(name=alias))
    except Exception as exc:
        run_storage_checkpoint(checkpoint)
        if _is_not_found(exc):
            return False
        _unavailable()
    run_storage_checkpoint(checkpoint)
    return exists


def _delete_document_from_target(
    manifest: DocumentDeletionManifest,
    *,
    client: SearchEngineClientProtocol,
    target: str,
    checkpoint: StorageCheckpoint,
    timeout_seconds: int,
) -> Mapping[str, Any] | None:
    run_storage_checkpoint(checkpoint)
    try:
        response = client.delete_by_query(
            index=target,
            body=build_document_delete_query(manifest.document_id),
            params={
                "conflicts": "proceed",
                "refresh": "true",
                "request_timeout": timeout_seconds,
                "wait_for_completion": "true",
            },
        )
    except Exception as exc:
        run_storage_checkpoint(checkpoint)
        if _is_not_found(exc):
            return None
        _unavailable()
    run_storage_checkpoint(checkpoint)
    return response


def _count_target(
    *,
    client: SearchEngineClientProtocol,
    target: str,
    body: dict[str, Any],
    checkpoint: StorageCheckpoint,
    timeout_seconds: int,
) -> int:
    run_storage_checkpoint(checkpoint)
    try:
        response = client.count(
            index=target,
            body=body,
            params={"request_timeout": timeout_seconds},
        )
    except Exception as exc:
        run_storage_checkpoint(checkpoint)
        if _is_not_found(exc):
            return 0
        _unavailable()
    run_storage_checkpoint(checkpoint)
    if not isinstance(response, Mapping):
        _unavailable()
    try:
        return int(response.get("count", -1))
    except (TypeError, ValueError):
        _unavailable()


def _is_not_found(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 404


def _unavailable() -> None:
    raise DocumentDeletionStorageError(
        DOCUMENT_DELETION_OPENSEARCH_UNAVAILABLE,
        "OpenSearch is unavailable during document deletion.",
    ) from None


def _delete_failed() -> None:
    raise DocumentDeletionStorageError(
        DOCUMENT_DELETION_OPENSEARCH_DELETE_FAILED,
        "OpenSearch delete-by-query did not complete.",
    )


def _incomplete() -> None:
    raise DocumentDeletionStorageError(
        DOCUMENT_DELETION_OPENSEARCH_INCOMPLETE,
        "OpenSearch verification found remaining document chunks.",
    )
