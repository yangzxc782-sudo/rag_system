from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, undefer

from app.core.config import get_settings
from app.core.errors import (
    DOCUMENT_ALREADY_PARSED,
    DOCUMENT_DELETION_IN_PROGRESS,
    DOCUMENT_DELETION_STATE_INCONSISTENT,
    DOCUMENT_DELETE_FAILED,
    DOCUMENT_NOT_FOUND,
    DOCUMENT_PARSE_FAILED,
    DOCUMENT_PARSER_CONFIG_INVALID,
    DOCUMENT_PARSER_UNAVAILABLE,
    DOCUMENT_PROCESSING_IN_PROGRESS,
    INVALID_FILE_TYPE,
    BusinessError,
)
from app.ingestion.file_types import DOCUMENT_FILE_EXTENSIONS, is_pdf_content
from app.ingestion.frozen_source import (
    CLEANER_VERSION, RENDERER_VERSION, json_bytes, sha256_bytes,
    render_frozen_source, verify_frozen_source,
)
from app.ingestion.pdf_cleaner import (
    PdfCleaningOptions,
    clean_pdf_blocks,
)
from app.ingestion.mineru.client import MinerUClient
from app.ingestion.mineru.models import (
    MinerUClientError,
    MinerUConfigError,
    MinerUParseRequest,
    MinerUParseResult,
    MinerURemoteError,
    MinerUTimeoutError,
)
from app.ingestion.mineru.normalizer import (
    MinerUNormalizationError,
    NormalizedDocumentAsset,
    NormalizedMinerUResult,
    normalize_mineru_result,
)
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.document_parse_run import DocumentParseRun
from app.services.document_assets import add_document_assets
from app.services.document_blocks import build_document_blocks
from app.models.document_source_version import SourceDocumentVersion
from app.services.document_sources import require_source_schema
from app.services.document_parse_runs import (
    create_parse_run,
    mark_failed,
    mark_running,
    mark_succeeded,
)
from app.services.document_operation_guard import DocumentOperationGuard
from app.services.object_storage import (
    get_object_bytes_from_minio,
    upload_bytes_to_minio,
)


@dataclass(frozen=True)
class DocumentParseResult:
    document_id: UUID
    process_status: str
    chunk_count: int
    parser_name: str
    parser_version: str
    parse_run_id: UUID
    source_version: UUID
    canonical_sha256: str
    character_count: int
    block_count: int


@dataclass(frozen=True)
class DocumentChunkListResult:
    items: list[DocumentChunk]
    total: int
    limit: int
    offset: int
    stats: dict[str, int | float]


@dataclass(frozen=True)
class _NormalizedAssetLookup:
    prefix: str
    by_source_path: dict[str, list[NormalizedDocumentAsset]]
    by_asset_key: dict[str, list[NormalizedDocumentAsset]]
    by_basename: dict[str, list[NormalizedDocumentAsset]]


@dataclass(frozen=True)
class _PendingMinerUUpload:
    object_key: str
    content: bytes = field(repr=False)
    content_type: str | None
    category: str


def parse_document(db: Session, document_id: UUID, *, settings=None, processing_lease=None) -> DocumentParseResult:
    document = _get_document_or_raise(db, document_id)
    extension = Path(document.original_filename).suffix.lower()
    if not extension:
        extension = (document.file_type or "").strip().lower()
        if extension and not extension.startswith("."):
            extension = f".{extension}"
    if extension not in DOCUMENT_FILE_EXTENSIONS:
        raise BusinessError(
            INVALID_FILE_TYPE,
            "仅支持解析 PDF 文档。",
            detail={"document_id": str(document_id), "extension": extension},
            status_code=415,
        )
    _ensure_document_has_no_chunks(db, document_id)
    settings = settings or get_settings()
    if settings.document_parser_provider != "mineru_api":
        raise BusinessError(
            DOCUMENT_PARSER_CONFIG_INVALID,
            "未支持的文档解析器配置。",
            detail={"document_parser_provider": settings.document_parser_provider},
            status_code=400,
        )
    return _parse_document_with_mineru(db, document, settings, processing_lease=processing_lease)


def _parse_document_with_mineru(
    db: Session, document: Document, settings: Any, *, processing_lease=None,
) -> DocumentParseResult:
    document_id = document.id
    document = DocumentOperationGuard(db).lock_normal(document_id)
    if processing_lease:
        processing_lease.check(db)
    else:
        from app.services.document_processing import reject_pending_parse
        reject_pending_parse(db, document_id)
    _ensure_document_has_no_chunks(db, document_id)
    if document.process_status == "parsing":
        raise BusinessError(DOCUMENT_PROCESSING_IN_PROGRESS, "文档正在解析，请等待当前任务结束。", status_code=409)
    if document.process_status in {"cleaned_source_ready", "kg_extracting", "kg_writing", "kg_failed", "kg_ready", "kg_ready_empty",
                                   "chunking", "chunks_ready", "embedding", "indexing", "retrieval_indexed", "retrieval_failed"}:
        raise BusinessError(DOCUMENT_ALREADY_PARSED, "文档已有冻结来源，不重复解析。", status_code=409)
    if not getattr(settings, "pdf_cleaning_enabled", True):
        raise BusinessError(DOCUMENT_PARSER_CONFIG_INVALID, "PDF 入库要求启用清洗，不能跳过清洗冻结。", status_code=400)
    require_source_schema(db)
    # Capture scalar input before commit. Expired ORM reads must not reopen a
    # transaction while model/storage IO is running.
    filename, bucket, object_key, mime_type = (
        document.original_filename, document.bucket_name, document.object_key, document.mime_type,
    )
    parse_run: DocumentParseRun | None = None
    stage = "create_parse_run"
    try:
        parse_run = create_parse_run(
            db, document_id=document_id, parser_provider="mineru_api", input_file_key=object_key,
            parse_mode=settings.mineru_parse_mode, output_base_prefix=settings.mineru_output_prefix,
            source_metadata={"parse_mode": settings.mineru_parse_mode},
        )
        parse_run_id = parse_run.id
        if processing_lease:
            processing_lease.started(db, parse_run_id)
        mark_running(db, parse_run)
        document.process_status = "parsing"
        document.error_message = None
        db.add(document)
        db.commit()

        stage = "source_file"
        content = get_object_bytes_from_minio(bucket_name=bucket, object_key=object_key)
        if not is_pdf_content(content):
            raise BusinessError(INVALID_FILE_TYPE, "文件内容不是 PDF。", status_code=415)
        stage = "mineru_client"
        parse_result = _create_mineru_client(settings).parse_file(MinerUParseRequest(
            filename=filename, content=content, mime_type=mime_type,
            parse_mode=settings.mineru_parse_mode, enable_ocr=settings.mineru_enable_ocr,
            save_intermediate=settings.mineru_save_intermediate,
        ))
        stage = "normalizer"
        normalized = normalize_mineru_result(
            parse_result, document_id=str(document_id), parse_run_id=str(parse_run_id),
            output_prefix=settings.mineru_output_prefix,
        )
        stage = "pdf_cleaning"
        options = PdfCleaningOptions(
            profile=getattr(settings, "pdf_cleaning_profile", "auto"),
            backfill_enabled=getattr(settings, "pdf_cleaning_backfill_enabled", True),
            filename=filename, zero_based_pages=parse_result.raw_metadata.get("api_version") == "v4",
        )
        blocks = clean_pdf_blocks(normalized.blocks, content, options)
        block_objects = build_document_blocks(document_id=document_id, parse_run_id=parse_run_id, blocks=blocks)
        for block in block_objects:
            block.id = uuid4()
        output_prefix = str(PurePosixPath(normalized.output_markdown_key).parent)
        source_version = uuid4()
        stage = "freeze_source"
        frozen = render_frozen_source(
            block_objects, document_id=document_id, parse_run_id=parse_run_id,
            source_version=source_version, output_prefix=output_prefix,
        )
        canonical_key, map_key = f"{output_prefix}/cleaned.md", f"{output_prefix}/source-map.json"
        assets = list(normalized.assets)
        if any(asset.asset_key in {canonical_key, map_key} for asset in assets):
            raise ValueError("Frozen source key collides with a raw asset")
        # Recheck after expensive work, then release the SQL lock before IO.
        # Deletion admission rejects 'parsing' until this attempt terminates.
        DocumentOperationGuard(db).lock_normal(document_id)
        if processing_lease:
            processing_lease.check(db)
            processing_lease.writing(db)
        parse_run = db.get(DocumentParseRun, parse_run_id) or parse_run
        parse_run.source_metadata = {**(parse_run.source_metadata or {}), "external_write_pending": True}
        db.commit()
        stage = "parsed_assets_storage"
        storage_metadata = _save_mineru_outputs(bucket_name=bucket, parse_result=parse_result, normalized=normalized)
        stage = "frozen_source_storage"
        for key, data, mime in ((canonical_key, frozen.canonical, "text/markdown"),
                                (map_key, frozen.block_map, "application/json")):
            upload_bytes_to_minio(bucket_name=bucket, object_key=key, content=data, content_type=mime)
            assets.append(NormalizedDocumentAsset(
                asset_type="markdown" if mime == "text/markdown" else "json", asset_key=key,
                filename=PurePosixPath(key).name, mime_type=mime, size_bytes=len(data),
            ))
        verify_frozen_source(
            get_object_bytes_from_minio(bucket_name=bucket, object_key=canonical_key),
            get_object_bytes_from_minio(bucket_name=bucket, object_key=map_key),
            source_version=source_version, document_id=document_id, parse_run_id=parse_run_id,
            canonical_sha256=frozen.canonical_sha256, block_map_sha256=frozen.block_map_sha256,
            character_count=frozen.character_count,
        )
        # All SQL writes finish in one short guarded transaction. No chunks are
        # created here; graph completion is a prerequisite of a later stage.
        stage = "source_persistence"
        document = DocumentOperationGuard(db).lock_normal(document_id)
        if processing_lease:
            processing_lease.check(db)
        _ensure_document_has_no_chunks(db, document_id)
        if document.process_status != "parsing":
            raise BusinessError(DOCUMENT_PROCESSING_IN_PROGRESS, "解析状态已改变，冻结提交被拒绝。", status_code=409)
        parse_run = db.get(DocumentParseRun, parse_run_id) or parse_run
        db.add_all(block_objects)
        add_document_assets(db, document_id=document_id, parse_run_id=parse_run_id, assets=assets)
        source = SourceDocumentVersion(
            source_version=source_version, document_id=document_id, parse_run_id=parse_run_id,
            bucket_name=bucket, canonical_object_key=canonical_key, canonical_sha256=frozen.canonical_sha256,
            character_count=frozen.character_count, block_map_object_key=map_key,
            block_map_sha256=frozen.block_map_sha256, cleaner_version=CLEANER_VERSION,
            renderer_version=RENDERER_VERSION, cleaning_config_sha256=sha256_bytes(json_bytes({
                "profile": options.profile, "backfill_enabled": options.backfill_enabled,
                "zero_based_pages": options.zero_based_pages, "filename": filename,
            })),
        )
        db.add(source)
        document.process_status = "cleaned_source_ready"
        document.error_message = None
        db.add(document)
        mark_succeeded(
            db, parse_run, parser_version=parse_result.parser_version,
            output_markdown_key=normalized.output_markdown_key, output_json_key=normalized.output_json_key,
            page_count=normalized.page_count, block_count=len(blocks), asset_count=len(assets),
            source_metadata={**normalized.source_metadata, **storage_metadata,
                             "source_version": str(source_version), "canonical_sha256": frozen.canonical_sha256,
                             "character_count": frozen.character_count, "chunk_count": 0},
        )
        db.flush()
        if processing_lease:
            processing_lease.succeeded(db, source_version)
        db.commit()
    except Exception as exc:
        db.rollback()
        if processing_lease:
            # Never record a stale attempt's failure over a newer source/lease.
            processing_lease.check(db)
        if isinstance(exc, BusinessError) and (
            _is_document_deletion_guard_error(exc)
            or exc.code in {DOCUMENT_NOT_FOUND, DOCUMENT_PROCESSING_IN_PROGRESS, DOCUMENT_ALREADY_PARSED}
        ):
            raise
        if parse_run is not None:
            _record_mineru_failure(db, document=document, parse_run=parse_run, stage=stage, error=exc, settings=settings)
        else:
            _mark_document_parse_failed(db, document_id, f"PDF 解析失败（阶段：{stage}）。")
        raise _mineru_business_error(document_id=document_id, stage=stage, error=exc) from exc
    return DocumentParseResult(
        document_id=document_id, process_status="cleaned_source_ready", chunk_count=0,
        parser_name=parse_result.parser_name, parser_version=parse_result.parser_version or "unknown",
        parse_run_id=parse_run_id, source_version=source_version, canonical_sha256=frozen.canonical_sha256,
        character_count=frozen.character_count, block_count=len(blocks),
    )


def list_document_chunks(
    db: Session,
    document_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
    chunk_set_id: UUID | None = None,
) -> DocumentChunkListResult:
    document = _get_document_or_raise(db, document_id)
    from app.services.versioned_document_guard import FROZEN_PROCESS_STATUSES
    from app.models.document_chunk_set import ChunkSet
    versioned = chunk_set_id is not None or document.process_status in FROZEN_PROCESS_STATUSES
    filters = [DocumentChunk.document_id == document_id]
    if versioned:
        chunk_set_id = chunk_set_id or document.current_chunk_set_id
        if chunk_set_id is not None and not db.scalar(select(ChunkSet.id).where(ChunkSet.id == chunk_set_id, ChunkSet.document_id == document_id)):
            raise BusinessError("CHUNK_SET_NOT_FOUND", "未找到该文档的切片版本。", status_code=404)
        # None means no published chunks; never expose the legacy rows instead.
        filters.append(DocumentChunk.chunk_set_id == chunk_set_id if chunk_set_id else DocumentChunk.id.is_(None))
    query = select(DocumentChunk).where(*filters)
    if versioned:
        query = query.options(undefer("*"))

    total = db.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(*filters),
    ) or 0
    items = list(
        db.scalars(
            query
            .order_by(DocumentChunk.chunk_index.asc())
            .limit(limit)
            .offset(offset),
        ).all(),
    )
    all_chunks = list(
        db.scalars(
            query
            .order_by(DocumentChunk.chunk_index.asc()),
        ).all(),
    )

    return DocumentChunkListResult(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        stats=_build_chunk_stats(all_chunks),
    )


def _get_document_or_raise(db: Session, document_id: UUID) -> Document:
    document = db.get(Document, document_id)

    if document is None:
        raise BusinessError(
            DOCUMENT_NOT_FOUND,
            "文档不存在。",
            detail={"document_id": str(document_id)},
            status_code=404,
        )

    return document


def _ensure_document_has_no_chunks(db: Session, document_id: UUID) -> None:
    chunk_count = db.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.document_id == document_id),
    ) or 0

    if chunk_count > 0:
        raise BusinessError(
            DOCUMENT_ALREADY_PARSED,
            "文档已存在切片，默认不重复解析。",
            detail={"document_id": str(document_id), "chunk_count": chunk_count},
            status_code=409,
        )


def _create_mineru_client(settings: Any) -> MinerUClient:
    return MinerUClient.from_settings(settings)


def _save_mineru_outputs(
    *,
    bucket_name: str,
    parse_result: MinerUParseResult,
    normalized: NormalizedMinerUResult,
) -> dict[str, Any]:
    asset_lookup = _build_normalized_asset_lookup(normalized)
    markdown_content = _result_file_content(
        parse_result,
        file_types={"markdown", "md", "output_markdown"},
    )
    if markdown_content is None and parse_result.markdown_text:
        markdown_content = parse_result.markdown_text.encode("utf-8")

    json_content = _result_file_content(
        parse_result,
        file_types={"json", "output_json"},
    )
    storage_metadata: dict[str, Any] = {
        "output_markdown_status": (
            "download_deferred"
            if _has_downloadable_result_file(
                parse_result,
                file_types={"markdown", "md", "output_markdown"},
            )
            else "unavailable"
        ),
        "output_json_status": (
            "download_deferred"
            if _has_downloadable_result_file(
                parse_result,
                file_types={"json", "output_json"},
            )
            else "unavailable"
        ),
        "intermediate_file_count": 0,
        "deferred_file_count": 0,
    }
    pending_uploads: list[_PendingMinerUUpload] = []
    planned_object_keys: set[str] = set()
    expected_uploaded_keys = {
        asset.asset_key
        for asset in normalized.assets
        if asset.size_bytes is not None
    }

    if markdown_content is not None:
        expected_uploaded_keys.add(normalized.output_markdown_key)
        _append_pending_mineru_upload(
            pending_uploads,
            planned_object_keys,
            object_key=normalized.output_markdown_key,
            content=markdown_content,
            content_type="text/markdown",
            category="output_markdown",
        )

    if json_content is not None:
        expected_uploaded_keys.add(normalized.output_json_key)
        _append_pending_mineru_upload(
            pending_uploads,
            planned_object_keys,
            object_key=normalized.output_json_key,
            content=json_content,
            content_type="application/json",
            category="output_json",
        )

    intermediate_count = 0
    deferred_count = 0
    for result_file in parse_result.result_files:
        if result_file.content is None:
            if result_file.download_url:
                deferred_count += 1
            continue
        file_type = result_file.file_type.strip().lower()
        if file_type in {
            "markdown",
            "md",
            "output_markdown",
            "json",
            "output_json",
        }:
            continue
        object_key = _resolve_normalized_asset_key(
            asset_lookup,
            source_path=result_file.source_path,
            asset_key=None,
            filename=result_file.filename,
        )
        expected_uploaded_keys.add(object_key)
        _append_pending_mineru_upload(
            pending_uploads,
            planned_object_keys,
            object_key=object_key,
            content=result_file.content,
            content_type=result_file.content_type,
            category="intermediate",
        )
        intermediate_count += 1

    for asset in parse_result.assets:
        if asset.content is None:
            if asset.download_url:
                deferred_count += 1
            continue
        object_key = _resolve_normalized_asset_key(
            asset_lookup,
            source_path=asset.source_path,
            asset_key=asset.asset_key,
            filename=asset.filename,
        )
        expected_uploaded_keys.add(object_key)
        _append_pending_mineru_upload(
            pending_uploads,
            planned_object_keys,
            object_key=object_key,
            content=asset.content,
            content_type=asset.mime_type,
            category="intermediate",
        )
        intermediate_count += 1

    if expected_uploaded_keys != planned_object_keys:
        raise ValueError("MinerU inline asset persistence is incomplete")

    uploaded_keys: set[str] = set()
    for pending in pending_uploads:
        upload_bytes_to_minio(
            bucket_name=bucket_name,
            object_key=pending.object_key,
            content=pending.content,
            content_type=pending.content_type,
        )
        uploaded_keys.add(pending.object_key)
        if pending.category == "output_markdown":
            storage_metadata["output_markdown_status"] = "saved"
        elif pending.category == "output_json":
            storage_metadata["output_json_status"] = "saved"

    if expected_uploaded_keys != uploaded_keys:
        raise ValueError("MinerU inline asset persistence is incomplete")

    storage_metadata["intermediate_file_count"] = intermediate_count
    storage_metadata["deferred_file_count"] = deferred_count
    return storage_metadata


def _append_pending_mineru_upload(
    pending_uploads: list[_PendingMinerUUpload],
    planned_object_keys: set[str],
    *,
    object_key: str,
    content: bytes,
    content_type: str | None,
    category: str,
) -> None:
    if object_key in planned_object_keys:
        raise ValueError("Multiple MinerU payloads resolved to the same asset key")
    planned_object_keys.add(object_key)
    pending_uploads.append(
        _PendingMinerUUpload(
            object_key=object_key,
            content=content,
            content_type=content_type,
            category=category,
        )
    )


def _result_file_content(
    parse_result: MinerUParseResult,
    *,
    file_types: set[str],
) -> bytes | None:
    for result_file in parse_result.result_files:
        if (
            result_file.file_type.strip().lower() in file_types
            and result_file.content is not None
        ):
            return result_file.content
    return None


def _has_downloadable_result_file(
    parse_result: MinerUParseResult,
    *,
    file_types: set[str],
) -> bool:
    return any(
        result_file.file_type.strip().lower() in file_types
        and bool(result_file.download_url)
        for result_file in parse_result.result_files
    )


def _build_normalized_asset_lookup(
    normalized: NormalizedMinerUResult,
) -> _NormalizedAssetLookup:
    output_markdown_key = _normalize_relative_posix_path(
        normalized.output_markdown_key
    )
    suffix = "/output.md"
    if not output_markdown_key.endswith(suffix):
        raise ValueError("Normalized MinerU output prefix is invalid")
    prefix = output_markdown_key[: -len(suffix)]
    if not prefix:
        raise ValueError("Normalized MinerU output prefix is invalid")

    by_source_path: dict[str, list[NormalizedDocumentAsset]] = {}
    by_asset_key: dict[str, list[NormalizedDocumentAsset]] = {}
    by_basename: dict[str, list[NormalizedDocumentAsset]] = {}
    exact_source_paths: set[str] = set()
    for asset in normalized.assets:
        normalized_asset_key = _normalize_relative_posix_path(asset.asset_key)
        if normalized_asset_key in by_asset_key:
            raise ValueError("Normalized MinerU assets contain a duplicate asset key")
        _append_asset_candidate(by_asset_key, normalized_asset_key, asset)
        basenames = {
            PurePosixPath(normalized_asset_key).name.casefold(),
            PurePosixPath(
                _normalize_relative_posix_path(asset.filename)
            ).name.casefold(),
        }
        for basename in basenames:
            _append_asset_candidate(by_basename, basename, asset)

        source_path = asset.source_metadata.get("source_path")
        if isinstance(source_path, str) and source_path.strip():
            normalized_source_path = _normalize_relative_posix_path(source_path)
            if normalized_source_path in exact_source_paths:
                raise ValueError(
                    "Normalized MinerU assets contain a duplicate source path"
                )
            exact_source_paths.add(normalized_source_path)
            for alias in _source_path_aliases(source_path):
                _append_asset_candidate(by_source_path, alias, asset)

    return _NormalizedAssetLookup(
        prefix=prefix,
        by_source_path=by_source_path,
        by_asset_key=by_asset_key,
        by_basename=by_basename,
    )


def _resolve_normalized_asset_key(
    lookup: _NormalizedAssetLookup,
    *,
    source_path: str | None,
    asset_key: str | None,
    filename: str,
) -> str:
    if source_path:
        for alias in _source_path_aliases(source_path):
            resolved = _unique_asset_candidate(
                lookup.by_source_path.get(alias, []),
                ambiguity_message="MinerU asset source path is ambiguous",
            )
            if resolved is not None:
                return resolved.asset_key

    if asset_key:
        relative_asset_key = _normalize_relative_posix_path(asset_key)
        normalized_asset_key = (
            relative_asset_key
            if relative_asset_key == lookup.prefix
            or relative_asset_key.startswith(f"{lookup.prefix}/")
            else f"{lookup.prefix}/{relative_asset_key}"
        )
        resolved = _unique_asset_candidate(
            lookup.by_asset_key.get(normalized_asset_key, []),
            ambiguity_message="MinerU asset key is ambiguous",
        )
        if resolved is not None:
            return resolved.asset_key

    normalized_filename = _normalize_relative_posix_path(filename)
    basename = PurePosixPath(normalized_filename).name.casefold()
    resolved = _unique_asset_candidate(
        lookup.by_basename.get(basename, []),
        ambiguity_message="MinerU asset filename is ambiguous",
    )
    if resolved is None:
        raise ValueError("MinerU inline asset has no normalized metadata match")
    return resolved.asset_key


def _append_asset_candidate(
    index: dict[str, list[NormalizedDocumentAsset]],
    key: str,
    asset: NormalizedDocumentAsset,
) -> None:
    index.setdefault(key, []).append(asset)


def _unique_asset_candidate(
    candidates: list[NormalizedDocumentAsset],
    *,
    ambiguity_message: str,
) -> NormalizedDocumentAsset | None:
    if not candidates:
        return None
    if len(candidates) > 1:
        raise ValueError(ambiguity_message)
    return candidates[0]


def _source_path_aliases(value: str) -> tuple[str, ...]:
    normalized = _normalize_relative_posix_path(value)
    aliases = [normalized]
    parts = PurePosixPath(normalized).parts
    for index, part in enumerate(parts):
        if part.casefold() == "images":
            aliases.append("/".join(parts[index:]))
            break
    return tuple(dict.fromkeys(aliases))


def _normalize_relative_posix_path(value: str) -> str:
    raw = str(value).strip().replace("\\", "/")
    path = PurePosixPath(raw)
    parts = path.parts
    if (
        not raw
        or raw.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".."} for part in parts)
        or (parts and parts[0].endswith(":"))
        or "\x00" in raw
    ):
        raise ValueError("MinerU asset path is invalid")
    normalized = "/".join(part for part in parts if part != ".")
    if not normalized:
        raise ValueError("MinerU asset path is invalid")
    return normalized


def _record_mineru_failure(
    db: Session,
    *,
    document: Document,
    parse_run: DocumentParseRun,
    stage: str,
    error: Exception,
    settings: Any,
) -> None:
    stored_document = DocumentOperationGuard(db).lock_normal(document.id)
    stored_parse_run = db.get(DocumentParseRun, parse_run.id) or parse_run
    if (stored_parse_run.source_metadata or {}).get("external_write_pending"):
        stored_parse_run.source_metadata = {**stored_parse_run.source_metadata, "requires_io_reconciliation": True}
    summary = (
        f"MinerU 文档解析失败（阶段：{stage}，"
        f"错误类型：{error.__class__.__name__}）。"
    )
    mark_failed(
        db,
        stored_parse_run,
        error_message=summary,
        secrets=(_mineru_api_key_value(settings),),
    )
    stored_document.process_status = "parse_failed"
    stored_document.error_message = stored_parse_run.error_message
    db.add(stored_document)
    try:
        db.commit()
    except Exception as persistence_error:
        db.rollback()
        stored_parse_run.status = "failed"
        stored_parse_run.is_active = False
        stored_document.process_status = "parse_failed"
        raise BusinessError(
            DOCUMENT_PARSE_FAILED,
            "文档解析失败，且失败状态未能可靠持久化。",
            detail={
                "document_id": str(document.id),
                "failure_status_persisted": False,
            },
            status_code=500,
        ) from persistence_error


def _mineru_business_error(
    *,
    document_id: UUID,
    stage: str,
    error: Exception,
) -> BusinessError:
    if isinstance(error, BusinessError):
        return error
    if isinstance(error, MinerUConfigError):
        return BusinessError(
            DOCUMENT_PARSER_CONFIG_INVALID,
            "MinerU API 配置无效。",
            detail={"document_id": str(document_id)},
            status_code=400,
        )
    if isinstance(error, (MinerUTimeoutError, MinerURemoteError)):
        return BusinessError(
            DOCUMENT_PARSER_UNAVAILABLE,
            "MinerU API 暂时不可用。",
            detail={
                "document_id": str(document_id),
                "error_type": error.__class__.__name__,
            },
            status_code=503,
        )
    if isinstance(error, (MinerUNormalizationError, MinerUClientError)):
        error_type = error.__class__.__name__
    else:
        error_type = error.__class__.__name__
    return BusinessError(
        DOCUMENT_PARSE_FAILED,
        "MinerU 文档解析入库失败。",
        detail={
            "document_id": str(document_id),
            "stage": stage,
            "error_type": error_type,
        },
        status_code=500,
    )


def _mineru_api_key_value(settings: Any) -> str:
    api_key = getattr(settings, "mineru_api_key", None)
    if api_key is None:
        return ""
    get_secret_value = getattr(api_key, "get_secret_value", None)
    if callable(get_secret_value):
        return str(get_secret_value())
    return str(api_key)


def _mark_document_parse_failed(db: Session, document_id: UUID, error_message: str) -> None:
    document = DocumentOperationGuard(db).lock_if_normal(document_id)
    if document is None:
        return

    document.process_status = "parse_failed"
    document.error_message = error_message
    db.add(document)
    db.commit()


def _is_document_deletion_guard_error(error: BusinessError) -> bool:
    return error.code in {
        DOCUMENT_DELETION_IN_PROGRESS,
        DOCUMENT_DELETE_FAILED,
        DOCUMENT_DELETION_STATE_INCONSISTENT,
    }


def _build_chunk_stats(chunks: list[DocumentChunk]) -> dict[str, int | float]:
    character_counts = [_get_character_count(chunk) for chunk in chunks]

    if not character_counts:
        return {
            "chunk_count": 0,
            "total_characters": 0,
            "min_characters": 0,
            "max_characters": 0,
            "avg_characters": 0,
        }

    total_characters = sum(character_counts)
    return {
        "chunk_count": len(character_counts),
        "total_characters": total_characters,
        "min_characters": min(character_counts),
        "max_characters": max(character_counts),
        "avg_characters": total_characters / len(character_counts),
    }


def _get_character_count(chunk: DocumentChunk) -> int:
    source_metadata: dict[str, Any] | None = chunk.source_metadata

    if source_metadata is not None:
        character_count = source_metadata.get("character_count")
        if isinstance(character_count, int):
            return character_count
        if isinstance(character_count, float):
            return int(character_count)

    return len(chunk.content)
