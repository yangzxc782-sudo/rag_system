from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app.core.errors import (
    DOCUMENT_ALREADY_PARSED,
    DOCUMENT_PARSE_FAILED,
    DOCUMENT_PARSER_CONFIG_INVALID,
    DOCUMENT_DELETION_IN_PROGRESS,
    BusinessError,
)
from app.ingestion.mineru.models import (
    MinerUAssetResult,
    MinerUParseRequest,
    MinerUParseResult,
    MinerUResultFile,
)
from app.ingestion.mineru.normalizer import (
    MinerUNormalizationError,
    NormalizedDocumentAsset,
)
from app.models.document import Document
from app.models.document_asset import DocumentAsset
from app.models.document_block import DocumentBlock
from app.models.document_chunk import DocumentChunk
from app.models.document_chunk_block import DocumentChunkBlock
from app.models.document_parse_run import DocumentParseRun
from app.services import document_parsing
from app.services.document_parse_runs import sanitize_error_message

DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
API_KEY = "mineru-test-secret"


class FakeScalarResult:
    def __init__(self, items) -> None:
        self._items = items

    def all(self):
        return self._items


class FakeDb:
    def __init__(
        self,
        *,
        document,
        chunk_count: int = 0,
        parse_runs: list[DocumentParseRun] | None = None,
        fail_initial_commit_once: bool = False,
        fail_failed_status_commit: bool = False,
    ) -> None:
        self.document = document
        self.chunk_count = chunk_count
        self.parse_runs = {
            parse_run.id: parse_run for parse_run in (parse_runs or [])
        }
        self.added: list[object] = []
        self.added_all: list[object] = []
        self.commits = 0
        self.commit_snapshots: list[dict[str, Any]] = []
        self.rollbacks = 0
        self.flushes = 0
        self.refreshed: list[object] = []
        self.fail_initial_commit_once = fail_initial_commit_once
        self.fail_failed_status_commit = fail_failed_status_commit
        self.initial_commit_failed = False
        self.failed_status_commit_attempts = 0

    def get(self, model, item_id):
        if model is Document:
            return self.document
        if model is DocumentParseRun:
            return self.parse_runs.get(item_id)
        return None

    def scalar(self, statement):
        if "FROM documents" in str(statement):
            return self.document
        return self.chunk_count

    def scalars(self, statement):
        return FakeScalarResult(list(self.parse_runs.values()))

    def add(self, item) -> None:
        self.added.append(item)
        if isinstance(item, DocumentParseRun):
            self.parse_runs[item.id] = item

    def add_all(self, items) -> None:
        self.added_all.extend(items)

    def flush(self) -> None:
        self.flushes += 1
        for item in [*self.added, *self.added_all]:
            if getattr(item, "id", None) is None:
                item.id = uuid4()

    def commit(self) -> None:
        self.commits += 1
        if self.fail_initial_commit_once and not self.initial_commit_failed:
            self.initial_commit_failed = True
            raise RuntimeError("simulated initial parse-run commit failure")
        if self.fail_failed_status_commit and any(
            run.status == "failed" for run in self.parse_runs.values()
        ):
            self.failed_status_commit_attempts += 1
            raise RuntimeError("simulated failed-status commit failure")
        self.commit_snapshots.append(
            {
                "document_status": self.document.process_status,
                "parse_runs": {
                    str(run.id): (run.status, run.is_active)
                    for run in self.parse_runs.values()
                },
            }
        )

    def rollback(self) -> None:
        self.rollbacks += 1

    def refresh(self, item) -> None:
        self.refreshed.append(item)


class FakeMinerUClient:
    def __init__(
        self,
        *,
        result: MinerUParseResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.requests: list[MinerUParseRequest] = []

    def parse_file(self, request: MinerUParseRequest) -> MinerUParseResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def fake_document() -> SimpleNamespace:
    return SimpleNamespace(
        id=DOCUMENT_ID,
        original_filename="casting.pdf",
        bucket_name="rag-documents",
        object_key=f"raw/2026/07/{DOCUMENT_ID}.pdf",
        file_type=".pdf",
        mime_type="application/pdf",
        process_status="uploaded",
        deletion_status="normal",
        error_message=None,
    )


def fake_settings(
    *,
    provider: str = "mineru_api",
    base_url: str | None = "https://mineru.invalid",
    api_key: SecretStr | None = SecretStr(API_KEY),
) -> SimpleNamespace:
    return SimpleNamespace(
        document_parser_provider=provider,
        chunk_size_chars=120,
        chunk_overlap_chars=0,
        mineru_api_base_url=base_url,
        mineru_api_key=api_key,
        mineru_api_timeout_seconds=300,
        mineru_api_poll_interval_seconds=5,
        mineru_api_max_poll_attempts=120,
        mineru_output_prefix="parsed-assets",
        mineru_parse_mode="auto",
        mineru_enable_ocr=True,
        mineru_save_intermediate=True,
    )


def fake_parse_result() -> MinerUParseResult:
    formula_term = (
        r"Q_{\mathrm{total}} = "
        r"\sum_{i=1}^{n} m_i c_i "
        r"\left(T_{\mathrm{pour}} - T_{\mathrm{mould}}\right)"
    )
    formula = f"{formula_term} + {formula_term}"
    table = "|A|B|\n|-|-|\n|1|2|\n|3|4|"
    return MinerUParseResult(
        parser_name="mineru_api",
        parser_version="test-v1",
        parse_mode="auto",
        markdown_text="# Casting",
        text="Casting",
        content_list=(
            {
                "type": "title",
                "id": "title-1",
                "level": 1,
                "text": "Casting",
                "page": 1,
            },
            {
                "type": "text",
                "id": "text-1",
                "text": "Prepare the mould before pouring.",
                "page": 1,
            },
            {
                "type": "formula",
                "id": "formula-1",
                "latex": formula,
                "page": 2,
            },
            {
                "type": "table",
                "id": "table-1",
                "markdown": table,
                "page": 3,
            },
        ),
        result_files=(
            MinerUResultFile(
                file_type="markdown",
                filename="output.md",
                content_type="text/markdown",
                content=b"# Casting",
            ),
            MinerUResultFile(
                file_type="json",
                filename="output.json",
                content_type="application/json",
                content=b'{"summary":"test"}',
            ),
        ),
        raw_metadata={"task_id": "fake-task", "api_version": "test"},
        page_count=3,
    )


def _install_success_dependencies(
    monkeypatch,
    *,
    client: FakeMinerUClient | None = None,
) -> tuple[FakeMinerUClient, list[dict[str, object]]]:
    fake_client = client or FakeMinerUClient(result=fake_parse_result())
    uploads: list[dict[str, object]] = []
    monkeypatch.setattr(
        document_parsing,
        "get_settings",
        lambda: fake_settings(),
    )
    monkeypatch.setattr(
        document_parsing,
        "get_object_bytes_from_minio",
        lambda **kwargs: b"%PDF-fake-content",
    )
    monkeypatch.setattr(
        document_parsing,
        "_create_mineru_client",
        lambda settings: fake_client,
    )
    monkeypatch.setattr(
        document_parsing,
        "upload_bytes_to_minio",
        lambda **kwargs: uploads.append(kwargs),
    )
    return fake_client, uploads


@pytest.mark.parametrize("extension", [".pdf", ".docx"])
def test_mineru_success_writes_full_pipeline_before_marking_active(
    monkeypatch, extension: str,
) -> None:
    old_run = DocumentParseRun(
        id=uuid4(),
        document_id=DOCUMENT_ID,
        parser_provider="mineru_api",
        status="succeeded",
        is_active=True,
    )
    document = fake_document()
    document.original_filename = "casting" + extension
    document.file_type = extension
    document.object_key = f"raw/2026/07/{DOCUMENT_ID}{extension}"
    db = FakeDb(document=document, parse_runs=[old_run])
    fake_client, uploads = _install_success_dependencies(monkeypatch)

    result = document_parsing.parse_document(db, DOCUMENT_ID)

    new_run = next(
        run for run in db.parse_runs.values() if run.id != old_run.id
    )
    assets = [
        item for item in db.added_all if isinstance(item, DocumentAsset)
    ]
    blocks = [
        item for item in db.added_all if isinstance(item, DocumentBlock)
    ]
    chunks = [
        item for item in db.added_all if isinstance(item, DocumentChunk)
    ]
    mappings = [
        item for item in db.added_all if isinstance(item, DocumentChunkBlock)
    ]

    assert result.process_status == "parsed"
    assert result.parser_name == "mineru_api"
    assert result.chunk_count == len(chunks)
    assert document.process_status == "parsed"
    assert new_run.status == "succeeded"
    assert new_run.is_active is True
    assert old_run.is_active is False
    assert new_run.page_count == 3
    assert new_run.block_count == 4
    assert new_run.asset_count == 2
    assert new_run.output_markdown_key.endswith("/output.md")
    assert new_run.output_json_key.endswith("/output.json")
    assert new_run.source_metadata["output_markdown_status"] == "saved"
    assert new_run.source_metadata["output_json_status"] == "saved"
    assert len(uploads) == 2
    assert len(fake_client.requests) == 1
    assert (
        db.commit_snapshots[0]["parse_runs"][str(new_run.id)]
        == ("running", False)
    )
    assert db.commit_snapshots[0]["document_status"] == "parsing"
    assert (
        db.commit_snapshots[-1]["parse_runs"][str(new_run.id)]
        == ("succeeded", True)
    )
    assert db.commit_snapshots[-1]["document_status"] == "parsed"
    assert len(assets) == 2
    assert len(blocks) == 4
    assert chunks
    assert mappings
    assert all(chunk.parse_run_id == new_run.id for chunk in chunks)
    assert all(chunk.chunk_method == "mineru_block_merge" for chunk in chunks)
    assert all(chunk.embedding is None for chunk in chunks)
    assert all(chunk.embedding_status == "not_started" for chunk in chunks)

    chunks_by_id = {chunk.id: chunk for chunk in chunks}
    formula_block = next(
        block for block in blocks if block.block_type == "formula"
    )
    table_block = next(
        block for block in blocks if block.block_type == "table"
    )
    formula_links = [
        mapping for mapping in mappings if mapping.block_id == formula_block.id
    ]
    table_links = [
        mapping for mapping in mappings if mapping.block_id == table_block.id
    ]
    assert len(formula_links) == 1
    assert len(table_links) == 1
    formula_chunk = chunks_by_id[formula_links[0].chunk_id]
    assert formula_block.latex in formula_chunk.content
    assert len(formula_chunk.content) > fake_settings().chunk_size_chars
    assert formula_chunk.chunk_type == "formula"
    assert table_block.markdown == chunks_by_id[table_links[0].chunk_id].content

    orders_by_chunk: dict[UUID, list[int]] = defaultdict(list)
    for mapping in mappings:
        orders_by_chunk[mapping.chunk_id].append(mapping.block_order)
    assert all(
        orders == list(range(len(orders)))
        for orders in orders_by_chunk.values()
    )


def test_mineru_final_guard_blocks_upload_after_delete_commits(monkeypatch) -> None:
    document = fake_document()

    class DeleteAfterRemoteParse(FakeMinerUClient):
        def parse_file(self, request):
            result = super().parse_file(request)
            document.deletion_status = "deleting"
            return result

    client = DeleteAfterRemoteParse(result=fake_parse_result())
    db = FakeDb(document=document)
    _client, uploads = _install_success_dependencies(monkeypatch, client=client)

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_DELETION_IN_PROGRESS
    assert uploads == []
    assert not any(isinstance(item, DocumentAsset) for item in db.added_all)


def test_inline_v4_zip_asset_is_uploaded_through_existing_orchestration(
    monkeypatch,
) -> None:
    base = fake_parse_result()
    parse_result = replace(
        base,
        parse_mode="vlm",
        assets=(
            MinerUAssetResult(
                asset_type="image",
                asset_key="images/mould.png",
                filename="mould.png",
                mime_type="image/png",
                page_number=1,
                caption="Mould layout",
                source_path="nested/images/mould.png",
                content=b"inline-image-bytes",
            ),
        ),
        raw_metadata={
            "batch_id": "batch-v4",
            "api_version": "v4",
            "final_status": "done",
            "parse_mode": "vlm",
            "file_count": 3,
            "asset_count": 1,
            "page_count": 3,
            "zip_file_types": ["content_list", "image", "markdown"],
        },
    )
    client = FakeMinerUClient(result=parse_result)
    document = fake_document()
    db = FakeDb(document=document)
    _, uploads = _install_success_dependencies(monkeypatch, client=client)

    result = document_parsing.parse_document(db, DOCUMENT_ID)

    image_upload = next(
        upload
        for upload in uploads
        if upload["object_key"].endswith("/images/mould.png")
    )
    assert result.process_status == "parsed"
    assert image_upload["content"] == b"inline-image-bytes"
    assert image_upload["content_type"] == "image/png"
    assert len(client.requests) == 1


def test_same_basename_assets_use_source_path_and_upload_to_distinct_keys(
    monkeypatch,
) -> None:
    parse_result = replace(
        fake_parse_result(),
        assets=(
            MinerUAssetResult(
                asset_type="image",
                asset_key="images/a/chart.png",
                filename="chart.png",
                mime_type="image/png",
                source_path="result/images/a/chart.png",
                content=b"chart-a",
            ),
            MinerUAssetResult(
                asset_type="image",
                asset_key="images/b/chart.png",
                filename="chart.png",
                mime_type="image/png",
                source_path="result/images/b/chart.png",
                content=b"chart-b",
            ),
        ),
    )
    client = FakeMinerUClient(result=parse_result)
    document = fake_document()
    db = FakeDb(document=document)
    _, uploads = _install_success_dependencies(monkeypatch, client=client)

    result = document_parsing.parse_document(db, DOCUMENT_ID)

    chart_uploads = {
        upload["object_key"]: upload["content"]
        for upload in uploads
        if str(upload["object_key"]).endswith("chart.png")
    }
    parse_run = next(iter(db.parse_runs.values()))
    assert result.process_status == "parsed"
    assert len(chart_uploads) == 2
    assert next(
        content
        for key, content in chart_uploads.items()
        if key.endswith("/images/a/chart.png")
    ) == b"chart-a"
    assert next(
        content
        for key, content in chart_uploads.items()
        if key.endswith("/images/b/chart.png")
    ) == b"chart-b"
    assert parse_run.status == "succeeded"
    assert parse_run.is_active is True


def test_zip_root_images_alias_matches_normalized_source_path(monkeypatch) -> None:
    parse_result = replace(
        fake_parse_result(),
        assets=(
            MinerUAssetResult(
                asset_type="image",
                asset_key="images/a/chart.png",
                filename="chart.png",
                mime_type="image/png",
                source_path="root/images/a/chart.png",
                content=b"root-prefixed-chart",
            ),
        ),
    )
    real_normalizer = document_parsing.normalize_mineru_result

    def normalized_without_zip_root(result, **kwargs):
        normalized = real_normalizer(result, **kwargs)
        image_asset = next(
            asset for asset in normalized.assets if asset.filename == "chart.png"
        )
        image_asset.source_metadata["source_path"] = "images/a/chart.png"
        return normalized

    document = fake_document()
    db = FakeDb(document=document)
    _, uploads = _install_success_dependencies(
        monkeypatch,
        client=FakeMinerUClient(result=parse_result),
    )
    monkeypatch.setattr(
        document_parsing,
        "normalize_mineru_result",
        normalized_without_zip_root,
    )

    result = document_parsing.parse_document(db, DOCUMENT_ID)

    image_upload = next(
        upload
        for upload in uploads
        if str(upload["object_key"]).endswith("/images/a/chart.png")
    )
    assert result.process_status == "parsed"
    assert image_upload["content"] == b"root-prefixed-chart"


def test_unique_basename_fallback_matches_inline_asset(monkeypatch) -> None:
    parse_result = replace(
        fake_parse_result(),
        assets=(
            MinerUAssetResult(
                asset_type="image",
                asset_key="",
                filename="unique-chart.png",
                mime_type="image/png",
                content=b"unique-chart",
            ),
        ),
    )
    document = fake_document()
    db = FakeDb(document=document)
    _, uploads = _install_success_dependencies(
        monkeypatch,
        client=FakeMinerUClient(result=parse_result),
    )

    result = document_parsing.parse_document(db, DOCUMENT_ID)

    image_upload = next(
        upload
        for upload in uploads
        if str(upload["object_key"]).endswith("/unique-chart.png")
    )
    assert result.process_status == "parsed"
    assert image_upload["content"] == b"unique-chart"


def test_duplicate_normalized_asset_key_fails_before_any_upload(
    monkeypatch,
) -> None:
    real_normalizer = document_parsing.normalize_mineru_result

    def duplicate_key_normalizer(result, **kwargs):
        normalized = real_normalizer(result, **kwargs)
        prefix = normalized.output_markdown_key.removesuffix("/output.md")
        duplicate_key = f"{prefix}/images/duplicate.png"
        assets = [
            *normalized.assets,
            NormalizedDocumentAsset(
                asset_type="image",
                asset_key=duplicate_key,
                filename="first.png",
                size_bytes=1,
                source_metadata={"source_path": "images/a/first.png"},
            ),
            NormalizedDocumentAsset(
                asset_type="image",
                asset_key=duplicate_key,
                filename="second.png",
                size_bytes=1,
                source_metadata={"source_path": "images/b/second.png"},
            ),
        ]
        return replace(normalized, assets=assets, asset_count=len(assets))

    document = fake_document()
    db = FakeDb(document=document)
    _, uploads = _install_success_dependencies(monkeypatch)
    monkeypatch.setattr(
        document_parsing,
        "normalize_mineru_result",
        duplicate_key_normalizer,
    )

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    parse_run = next(iter(db.parse_runs.values()))
    assert exc_info.value.code == DOCUMENT_PARSE_FAILED
    assert uploads == []
    assert parse_run.status == "failed"
    assert parse_run.is_active is False
    assert document.process_status == "parse_failed"


def test_duplicate_normalized_source_path_fails_before_any_upload(
    monkeypatch,
) -> None:
    real_normalizer = document_parsing.normalize_mineru_result

    def duplicate_source_normalizer(result, **kwargs):
        normalized = real_normalizer(result, **kwargs)
        prefix = normalized.output_markdown_key.removesuffix("/output.md")
        assets = [
            *normalized.assets,
            NormalizedDocumentAsset(
                asset_type="image",
                asset_key=f"{prefix}/images/a/first.png",
                filename="first.png",
                size_bytes=1,
                source_metadata={"source_path": "root/images/shared.png"},
            ),
            NormalizedDocumentAsset(
                asset_type="image",
                asset_key=f"{prefix}/images/b/second.png",
                filename="second.png",
                size_bytes=1,
                source_metadata={"source_path": "root/images/shared.png"},
            ),
        ]
        return replace(normalized, assets=assets, asset_count=len(assets))

    document = fake_document()
    db = FakeDb(document=document)
    _, uploads = _install_success_dependencies(monkeypatch)
    monkeypatch.setattr(
        document_parsing,
        "normalize_mineru_result",
        duplicate_source_normalizer,
    )

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    parse_run = next(iter(db.parse_runs.values()))
    assert exc_info.value.code == DOCUMENT_PARSE_FAILED
    assert uploads == []
    assert parse_run.status == "failed"
    assert parse_run.is_active is False
    assert document.process_status == "parse_failed"


def test_ambiguous_basename_without_source_path_fails_parse(
    monkeypatch,
) -> None:
    parse_result = replace(
        fake_parse_result(),
        assets=(
            MinerUAssetResult(
                asset_type="image",
                asset_key="chart.png",
                filename="chart.png",
                mime_type="image/png",
                content=b"ambiguous-chart",
            ),
        ),
    )
    real_normalizer = document_parsing.normalize_mineru_result

    def ambiguous_normalizer(result, **kwargs):
        normalized = real_normalizer(result, **kwargs)
        prefix = normalized.output_markdown_key.removesuffix("/output.md")
        assets = [
            asset
            for asset in normalized.assets
            if asset.filename != "chart.png"
        ]
        assets.extend(
            [
                NormalizedDocumentAsset(
                    asset_type="image",
                    asset_key=f"{prefix}/images/a/chart.png",
                    filename="chart.png",
                    size_bytes=len(b"ambiguous-chart"),
                ),
                NormalizedDocumentAsset(
                    asset_type="image",
                    asset_key=f"{prefix}/images/b/chart.png",
                    filename="chart.png",
                    size_bytes=len(b"ambiguous-chart"),
                ),
            ]
        )
        return replace(
            normalized,
            assets=assets,
            asset_count=len(assets),
        )

    document = fake_document()
    db = FakeDb(document=document)
    _install_success_dependencies(
        monkeypatch,
        client=FakeMinerUClient(result=parse_result),
    )
    monkeypatch.setattr(
        document_parsing,
        "normalize_mineru_result",
        ambiguous_normalizer,
    )

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    parse_run = next(iter(db.parse_runs.values()))
    assert exc_info.value.code == DOCUMENT_PARSE_FAILED
    assert parse_run.status == "failed"
    assert parse_run.is_active is False
    assert document.process_status == "parse_failed"


def test_missing_declared_inline_asset_fails_integrity_check(
    monkeypatch,
) -> None:
    parse_result = replace(
        fake_parse_result(),
        assets=(
            MinerUAssetResult(
                asset_type="image",
                asset_key="images/a/chart.png",
                filename="chart.png",
                mime_type="image/png",
                source_path="result/images/a/chart.png",
                content=b"chart-a",
            ),
        ),
    )
    real_normalizer = document_parsing.normalize_mineru_result

    def incomplete_normalizer(result, **kwargs):
        normalized = real_normalizer(result, **kwargs)
        prefix = normalized.output_markdown_key.removesuffix("/output.md")
        assets = [
            *normalized.assets,
            NormalizedDocumentAsset(
                asset_type="image",
                asset_key=f"{prefix}/images/b/chart.png",
                filename="chart.png",
                size_bytes=len(b"missing-chart"),
                source_metadata={"source_path": "result/images/b/chart.png"},
            ),
        ]
        return replace(
            normalized,
            assets=assets,
            asset_count=len(assets),
        )

    document = fake_document()
    db = FakeDb(document=document)
    _install_success_dependencies(
        monkeypatch,
        client=FakeMinerUClient(result=parse_result),
    )
    monkeypatch.setattr(
        document_parsing,
        "normalize_mineru_result",
        incomplete_normalizer,
    )

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    parse_run = next(iter(db.parse_runs.values()))
    assert exc_info.value.code == DOCUMENT_PARSE_FAILED
    assert parse_run.status == "failed"
    assert parse_run.is_active is False
    assert document.process_status == "parse_failed"


def test_mineru_missing_configuration_fails_without_basic_fallback(
    monkeypatch,
) -> None:
    document = fake_document()
    db = FakeDb(document=document)
    monkeypatch.setattr(
        document_parsing,
        "get_settings",
        lambda: fake_settings(base_url=None, api_key=None),
    )
    monkeypatch.setattr(
        document_parsing,
        "get_object_bytes_from_minio",
        lambda **kwargs: b"%PDF",
    )

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    parse_run = next(iter(db.parse_runs.values()))
    assert exc_info.value.code == DOCUMENT_PARSER_CONFIG_INVALID
    assert parse_run.status == "failed"
    assert parse_run.is_active is False
    assert document.process_status == "parse_failed"


def test_initial_parse_run_commit_failure_can_still_persist_failed_state(
    monkeypatch,
) -> None:
    document = fake_document()
    db = FakeDb(document=document, fail_initial_commit_once=True)
    fake_client, _ = _install_success_dependencies(monkeypatch)

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    parse_run = next(iter(db.parse_runs.values()))
    assert exc_info.value.code == DOCUMENT_PARSE_FAILED
    assert parse_run.status == "failed"
    assert parse_run.is_active is False
    assert document.process_status == "parse_failed"
    assert db.initial_commit_failed is True
    assert db.commit_snapshots[-1]["parse_runs"][str(parse_run.id)] == (
        "failed",
        False,
    )
    assert fake_client.requests == []


def test_existing_chunks_still_prevent_mineru_reparse(monkeypatch) -> None:
    document = fake_document()
    db = FakeDb(document=document, chunk_count=1)
    client_called = False

    def fail_if_called(settings):
        nonlocal client_called
        client_called = True
        raise AssertionError("MinerU client must not be created")

    monkeypatch.setattr(
        document_parsing,
        "_create_mineru_client",
        fail_if_called,
    )

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    assert exc_info.value.code == DOCUMENT_ALREADY_PARSED
    assert client_called is False
    assert db.parse_runs == {}
    assert db.commits == 0


def test_parse_run_error_message_is_redacted_and_truncated() -> None:
    message = f"{API_KEY} " + ("sensitive-detail " * 100)

    sanitized = sanitize_error_message(
        message,
        secrets=(API_KEY,),
        max_length=120,
    )

    assert API_KEY not in sanitized
    assert len(sanitized) <= 120


def test_download_only_outputs_are_marked_deferred_without_network(
    monkeypatch,
) -> None:
    base = fake_parse_result()
    deferred_result = MinerUParseResult(
        parser_name=base.parser_name,
        parser_version=base.parser_version,
        parse_mode=base.parse_mode,
        markdown_text="",
        text=base.text,
        content_list=base.content_list,
        result_files=(
            MinerUResultFile(
                file_type="markdown",
                filename="output.md",
                content_type="text/markdown",
                download_url="https://mineru.invalid/results/output.md",
            ),
            MinerUResultFile(
                file_type="json",
                filename="output.json",
                content_type="application/json",
                download_url="https://mineru.invalid/results/output.json",
            ),
        ),
        raw_metadata=base.raw_metadata,
        page_count=base.page_count,
    )
    client = FakeMinerUClient(result=deferred_result)
    document = fake_document()
    db = FakeDb(document=document)
    _, uploads = _install_success_dependencies(
        monkeypatch,
        client=client,
    )

    result = document_parsing.parse_document(db, DOCUMENT_ID)

    parse_run = next(iter(db.parse_runs.values()))
    assert result.process_status == "parsed"
    assert parse_run.status == "succeeded"
    assert parse_run.is_active is True
    assert parse_run.source_metadata["output_markdown_status"] == (
        "download_deferred"
    )
    assert parse_run.source_metadata["output_json_status"] == (
        "download_deferred"
    )
    assert parse_run.source_metadata["deferred_file_count"] == 2
    assert parse_run.output_markdown_key.endswith("/output.md")
    assert parse_run.output_json_key.endswith("/output.json")
    assert uploads == []


def test_failed_status_commit_failure_is_visible_to_caller(
    monkeypatch,
) -> None:
    document = fake_document()
    db = FakeDb(document=document, fail_failed_status_commit=True)
    _install_success_dependencies(monkeypatch)
    monkeypatch.setattr(
        document_parsing,
        "normalize_mineru_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            MinerUNormalizationError("normalizer failed")
        ),
    )

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    parse_run = next(iter(db.parse_runs.values()))
    assert exc_info.value.code == DOCUMENT_PARSE_FAILED
    assert "失败状态未能可靠持久化" in exc_info.value.message
    assert exc_info.value.detail["failure_status_persisted"] is False
    assert db.failed_status_commit_attempts == 1
    assert db.rollbacks >= 2
    assert document.process_status != "parsed"
    assert parse_run.status != "succeeded"
    assert parse_run.is_active is False


@pytest.mark.parametrize(
    "failure_point",
    [
        "storage",
        "normalizer",
        "assets",
        "blocks",
        "chunking",
        "chunks",
        "mappings",
    ],
)
def test_mineru_pipeline_failure_marks_run_and_document_failed(
    monkeypatch,
    failure_point: str,
) -> None:
    document = fake_document()
    db = FakeDb(document=document)
    _install_success_dependencies(monkeypatch)
    raw_document = "complete-original-document"
    raw_json = '{"full":"mineru-json"}'

    def fail() -> None:
        raise RuntimeError(f"{API_KEY} {raw_document} {raw_json}")

    if failure_point == "storage":
        monkeypatch.setattr(
            document_parsing,
            "upload_bytes_to_minio",
            lambda **kwargs: fail(),
        )
    elif failure_point == "normalizer":
        monkeypatch.setattr(
            document_parsing,
            "normalize_mineru_result",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                MinerUNormalizationError(
                    f"{API_KEY} {raw_document} {raw_json}"
                )
            ),
        )
    elif failure_point == "assets":
        monkeypatch.setattr(
            document_parsing,
            "add_document_assets",
            lambda *args, **kwargs: fail(),
        )
    elif failure_point == "blocks":
        monkeypatch.setattr(
            document_parsing,
            "add_document_blocks",
            lambda *args, **kwargs: fail(),
        )
    elif failure_point == "chunking":
        monkeypatch.setattr(
            document_parsing,
            "build_block_aware_chunks",
            lambda *args, **kwargs: fail(),
        )
    elif failure_point == "chunks":
        monkeypatch.setattr(
            document_parsing,
            "_add_mineru_chunks",
            lambda *args, **kwargs: fail(),
        )
    else:
        monkeypatch.setattr(
            document_parsing,
            "_add_chunk_block_mappings",
            lambda *args, **kwargs: fail(),
        )

    with pytest.raises(BusinessError) as exc_info:
        document_parsing.parse_document(db, DOCUMENT_ID)

    parse_run = next(iter(db.parse_runs.values()))
    assert exc_info.value.code == DOCUMENT_PARSE_FAILED
    assert db.rollbacks >= 1
    assert parse_run.status == "failed"
    assert parse_run.is_active is False
    assert document.process_status == "parse_failed"
    assert db.commit_snapshots[-1]["document_status"] == "parse_failed"
    assert (
        db.commit_snapshots[-1]["parse_runs"][str(parse_run.id)]
        == ("failed", False)
    )
    assert API_KEY not in (parse_run.error_message or "")
    assert raw_document not in (parse_run.error_message or "")
    assert raw_json not in (parse_run.error_message or "")
