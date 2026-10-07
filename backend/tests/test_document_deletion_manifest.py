from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.services.document_deletion_manifest import (
    DOCUMENT_DELETION_MANIFEST_INVALID,
    DOCUMENT_DELETION_MANIFEST_VERSION_UNSUPPORTED,
    DocumentDeletionManifest,
    DocumentDeletionManifestError,
    build_document_deletion_manifest,
)


DOCUMENT_ID = UUID("11111111-1111-1111-1111-111111111111")
PARSE_RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
CHUNK_ID = UUID("33333333-3333-3333-3333-333333333333")
BLOCK_ID = UUID("44444444-4444-4444-4444-444444444444")
ASSET_ID = UUID("55555555-5555-5555-5555-555555555555")
SOURCE_ID = UUID("66666666-6666-6666-6666-666666666666")
KNOWLEDGE_ITEM_ID = UUID("77777777-7777-7777-7777-777777777777")
PREFIX = f"parsed-assets/{DOCUMENT_ID}/{PARSE_RUN_ID}"


def valid_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "document_id": str(DOCUMENT_ID),
        "bucket_name": "rag-documents",
        "raw_object_key": f"raw/2026/08/{DOCUMENT_ID}.pdf",
        "derived_object_keys": [
            f"{PREFIX}/images/mould.png",
            f"{PREFIX}/output.json",
            f"{PREFIX}/output.md",
        ],
        "derived_prefixes": [f"{PREFIX}/"],
        "parse_run_ids": [str(PARSE_RUN_ID)],
        "block_ids": [str(BLOCK_ID)],
        "asset_ids": [str(ASSET_ID)],
        "chunk_ids": [str(CHUNK_ID)],
        "knowledge_source_relation_ids": [str(SOURCE_ID)],
        "knowledge_item_ids": [str(KNOWLEDGE_ITEM_ID)],
        "search_index_name": "casting_chunks_v1",
        "search_index_alias": "casting_chunks_current",
    }


def fake_document(**overrides: object) -> SimpleNamespace:
    parse_run = SimpleNamespace(
        id=PARSE_RUN_ID,
        output_prefix=PREFIX,
        output_markdown_key=f"{PREFIX}/output.md",
        output_json_key=f"{PREFIX}/output.json",
    )
    values: dict[str, object] = {
        "id": DOCUMENT_ID,
        "bucket_name": "rag-documents",
        "object_key": f"raw/2026/08/{DOCUMENT_ID}.pdf",
        "parse_runs": [parse_run],
        "blocks": [SimpleNamespace(id=BLOCK_ID)],
        "assets": [
            SimpleNamespace(
                id=ASSET_ID,
                parse_run_id=PARSE_RUN_ID,
                asset_key=f"{PREFIX}/images/mould.png",
            )
        ],
        "chunks": [SimpleNamespace(id=CHUNK_ID)],
        "knowledge_item_sources": [
            SimpleNamespace(id=SOURCE_ID, knowledge_item_id=KNOWLEDGE_ITEM_ID)
        ],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def fake_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "mineru_output_prefix": "parsed-assets",
        "search_index_name": "casting_chunks_v1",
        "search_index_alias": "casting_chunks_current",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_manifest_roundtrip_is_versioned_strict_and_deterministic() -> None:
    payload = valid_payload()
    payload["chunk_ids"] = [str(CHUNK_ID), str(CHUNK_ID)]
    payload["derived_object_keys"] = [
        f"{PREFIX}/output.md",
        f"{PREFIX}/images/mould.png",
        f"{PREFIX}/output.md",
    ]

    manifest = DocumentDeletionManifest.from_payload(payload)

    expected = valid_payload()
    expected["derived_object_keys"] = [
        f"{PREFIX}/images/mould.png",
        f"{PREFIX}/output.md",
    ]
    assert manifest.to_payload() == expected
    assert DocumentDeletionManifest.from_payload(manifest.to_payload()) == manifest


def test_manifest_builder_uses_persisted_exact_keys_and_all_recovery_ids() -> None:
    document = fake_document()

    manifest = build_document_deletion_manifest(
        SimpleNamespace(),
        document=document,
        settings=fake_settings(),
    )

    assert manifest.raw_object_key == document.object_key
    assert manifest.derived_prefixes == (f"{PREFIX}/",)
    assert manifest.derived_object_keys == (
        f"{PREFIX}/images/mould.png",
        f"{PREFIX}/output.json",
        f"{PREFIX}/output.md",
    )
    assert manifest.parse_run_ids == (PARSE_RUN_ID,)
    assert manifest.block_ids == (BLOCK_ID,)
    assert manifest.asset_ids == (ASSET_ID,)
    assert manifest.chunk_ids == (CHUNK_ID,)
    assert manifest.knowledge_source_relation_ids == (SOURCE_ID,)
    assert manifest.knowledge_item_ids == (KNOWLEDGE_ITEM_ID,)


def test_unknown_manifest_version_fails_without_guessing() -> None:
    payload = valid_payload()
    payload["schema_version"] = 99

    with pytest.raises(DocumentDeletionManifestError) as exc_info:
        DocumentDeletionManifest.from_payload(payload)

    assert exc_info.value.code == DOCUMENT_DELETION_MANIFEST_VERSION_UNSUPPORTED


def test_boolean_manifest_version_is_not_accepted_as_integer_one() -> None:
    payload = valid_payload()
    payload["schema_version"] = True

    with pytest.raises(DocumentDeletionManifestError) as exc_info:
        DocumentDeletionManifest.from_payload(payload)

    assert exc_info.value.code == DOCUMENT_DELETION_MANIFEST_VERSION_UNSUPPORTED


@pytest.mark.parametrize(
    "field",
    [
        "document_text",
        "chunk_text",
        "source_text",
        "knowledge_item_content",
        "embedding",
        "image_bytes",
        "prompt",
        "api_key",
        "minio_secret_key",
        "opensearch_password",
        "stack_trace",
        "external_error_body",
    ],
)
def test_manifest_rejects_sensitive_or_unknown_fields(field: str) -> None:
    payload = valid_payload()
    payload[field] = "must-not-persist"

    with pytest.raises(DocumentDeletionManifestError) as exc_info:
        DocumentDeletionManifest.from_payload(payload)

    assert exc_info.value.code == DOCUMENT_DELETION_MANIFEST_INVALID
    assert "must-not-persist" not in str(exc_info.value)


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "/",
        "parsed-assets/",
        f"parsed-assets/{DOCUMENT_ID}/../sibling/",
        f"parsed-assets/{DOCUMENT_ID}/%2e%2e/sibling/",
        f"parsed-assets/{DOCUMENT_ID}/%252e%252e/sibling/",
        f"parsed-assets/99999999-9999-9999-9999-999999999999/{PARSE_RUN_ID}/",
        f"parsed-assets/{DOCUMENT_ID}/88888888-8888-8888-8888-888888888888/",
    ],
)
def test_manifest_rejects_unsafe_or_wrong_document_prefix(prefix: str) -> None:
    payload = valid_payload()
    payload["derived_prefixes"] = [prefix]

    with pytest.raises(DocumentDeletionManifestError) as exc_info:
        DocumentDeletionManifest.from_payload(payload)

    assert exc_info.value.code == DOCUMENT_DELETION_MANIFEST_INVALID


def test_document_root_prefix_is_valid_only_for_the_captured_document() -> None:
    payload = valid_payload()
    payload["derived_object_keys"] = []
    payload["derived_prefixes"] = [f"parsed-assets/{DOCUMENT_ID}/"]

    manifest = DocumentDeletionManifest.from_payload(payload)

    assert manifest.derived_prefixes == (f"parsed-assets/{DOCUMENT_ID}/",)


@pytest.mark.parametrize(
    "raw_key",
    [
        "",
        "/",
        f"raw/2026/08/99999999-9999-9999-9999-999999999999.pdf",
        f"raw/2026/08/../{DOCUMENT_ID}.pdf",
        f"raw/2026/08/%2e%2e/{DOCUMENT_ID}.pdf",
        f"raw/2026/08/{DOCUMENT_ID}",
        f"parsed-assets/{DOCUMENT_ID}/file.pdf",
    ],
)
def test_manifest_requires_exact_raw_document_key(raw_key: str) -> None:
    payload = valid_payload()
    payload["raw_object_key"] = raw_key

    with pytest.raises(DocumentDeletionManifestError) as exc_info:
        DocumentDeletionManifest.from_payload(payload)

    assert exc_info.value.code == DOCUMENT_DELETION_MANIFEST_INVALID


def test_manifest_rejects_derived_key_outside_validated_prefix() -> None:
    payload = valid_payload()
    payload["derived_object_keys"] = [
        "parsed-assets/99999999-9999-9999-9999-999999999999/output.md"
    ]

    with pytest.raises(DocumentDeletionManifestError) as exc_info:
        DocumentDeletionManifest.from_payload(payload)

    assert exc_info.value.code == DOCUMENT_DELETION_MANIFEST_INVALID


def test_manifest_builder_rejects_persisted_prefix_outside_configured_root() -> None:
    document = fake_document()
    document.parse_runs[0].output_prefix = f"other-root/{DOCUMENT_ID}/{PARSE_RUN_ID}"

    with pytest.raises(DocumentDeletionManifestError) as exc_info:
        build_document_deletion_manifest(
            SimpleNamespace(),
            document=document,
            settings=fake_settings(),
        )

    assert exc_info.value.code == DOCUMENT_DELETION_MANIFEST_INVALID


def test_manifest_builder_rejects_parse_run_without_recovery_prefix() -> None:
    document = fake_document()
    document.parse_runs[0].output_prefix = None
    document.parse_runs[0].output_markdown_key = None
    document.parse_runs[0].output_json_key = None
    document.assets = []

    with pytest.raises(DocumentDeletionManifestError) as exc_info:
        build_document_deletion_manifest(
            SimpleNamespace(),
            document=document,
            settings=fake_settings(),
        )

    assert exc_info.value.code == DOCUMENT_DELETION_MANIFEST_INVALID


def test_manifest_payload_does_not_mutate_the_callers_data() -> None:
    payload = valid_payload()
    original = deepcopy(payload)

    DocumentDeletionManifest.from_payload(payload)

    assert payload == original
