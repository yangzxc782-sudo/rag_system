from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
    INVALID_FILE_TYPE,
    BusinessError,
)
from app.ingestion.file_types import MARKDOWN_FILE_EXTENSION, MINERU_FILE_EXTENSIONS
from app.ingestion.block_chunker import (
    BlockChunkerConfig,
    BuiltChunkBlockLink,
    BuiltDocumentChunk,
    build_block_aware_chunks,
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
from app.models.document_chunk_block import DocumentChunkBlock
from app.models.document_parse_run import DocumentParseRun
from app.services.document_assets import add_document_assets
from app.services.document_blocks import add_document_blocks
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


def parse_document(db: Session, document_id: UUID) -> DocumentParseResult:
    document = _get_document_or_raise(db, document_id)
    _ensure_document_has_no_chunks(db, document_id)
    extension = Path(document.original_filename).suffix.lower()
    if not extension:
        extension = (document.file_type or "").strip().lower()
        if extension and not extension.startswith("."):
            extension = f".{extension}"
    if extension == MARKDOWN_FILE_EXTENSION:
        raise BusinessError(
            DOCUMENT_PARSER_UNAVAILABLE,
            "Markdown 原生解析尚未开放，已上传的文档可保留等待后续解析。",
            detail={"document_id": str(document_id), "extension": extension},
            status_code=503,
        )
    if extension not in MINERU_FILE_EXTENSIONS:
        raise BusinessError(
            INVALID_FILE_TYPE,
            "此文件类型没有受支持的文档解析路径。",
            detail={"document_id": str(document_id), "extension": extension},
            status_code=415,
        )
    settings = get_settings()
    if settings.document_parser_provider != "mineru_api":
        raise BusinessError(
            DOCUMENT_PARSER_CONFIG_INVALID,
            "未支持的文档解析器配置。",
            detail={"document_parser_provider": settings.document_parser_provider},
            status_code=400,
        )
    return _parse_document_with_mineru(db, document, settings)


def _parse_document_with_mineru(
    db: Session,
    document: Document,
    settings: Any,
) -> DocumentParseResult:
    parse_run: DocumentParseRun | None = None
    stage = "create_parse_run"

    try:
        document = DocumentOperationGuard(db).lock_normal(document.id)
        parse_run = create_parse_run(
            db,
            document_id=document.id,
            parser_provider="mineru_api",
            input_file_key=document.object_key,
            parse_mode=settings.mineru_parse_mode,
            output_base_prefix=settings.mineru_output_prefix,
            source_metadata={"parse_mode": settings.mineru_parse_mode},
        )
        mark_running(db, parse_run)
        document.process_status = "parsing"
        document.error_message = None
        db.add(document)
        db.commit()
        stage = "source_file"
        content = get_object_bytes_from_minio(
            bucket_name=document.bucket_name,
            object_key=document.object_key,
        )

        stage = "mineru_client"
        client = _create_mineru_client(settings)
        parse_result = client.parse_file(
            MinerUParseRequest(
                filename=document.original_filename,
                content=content,
                mime_type=document.mime_type,
                parse_mode=settings.mineru_parse_mode,
                enable_ocr=settings.mineru_enable_ocr,
                save_intermediate=settings.mineru_save_intermediate,
            )
        )

        stage = "normalizer"
        normalized = normalize_mineru_result(
            parse_result,
            document_id=str(document.id),
            parse_run_id=str(parse_run.id),
            output_prefix=settings.mineru_output_prefix,
        )

        stage = "parsed_assets_storage"
        document = DocumentOperationGuard(db).lock_normal(document.id)
        storage_metadata = _save_mineru_outputs(
            bucket_name=document.bucket_name,
            parse_result=parse_result,
            normalized=normalized,
        )

        stage = "document_assets"
        add_document_assets(
            db,
            document_id=document.id,
            parse_run_id=parse_run.id,
            assets=normalized.assets,
        )

        stage = "document_blocks"
        block_objects = add_document_blocks(
            db,
            document_id=document.id,
            parse_run_id=parse_run.id,
            blocks=normalized.blocks,
        )

        stage = "block_chunking"
        chunk_build = build_block_aware_chunks(
            block_objects,
            parse_run_id=str(parse_run.id),
            config=_mineru_chunker_config(settings),
        )

        stage = "document_chunks"
        chunk_objects = _add_mineru_chunks(
            db,
            document_id=document.id,
            parse_run_id=parse_run.id,
            chunks=chunk_build.chunks,
        )

        stage = "document_chunk_blocks"
        _add_chunk_block_mappings(
            db,
            chunks=chunk_objects,
            links=chunk_build.links,
        )

        stage = "complete"
        document.process_status = "parsed"
        document.error_message = None
        db.add(document)
        mark_succeeded(
            db,
            parse_run,
            parser_version=parse_result.parser_version,
            output_markdown_key=normalized.output_markdown_key,
            output_json_key=normalized.output_json_key,
            page_count=normalized.page_count,
            block_count=normalized.block_count,
            asset_count=normalized.asset_count,
            source_metadata={
                **normalized.source_metadata,
                **storage_metadata,
            },
        )
        db.commit()
        db.refresh(document)
        db.refresh(parse_run)
    except Exception as exc:
        db.rollback()
        if isinstance(exc, BusinessError) and _is_document_deletion_guard_error(exc):
            raise
        if parse_run is not None:
            _record_mineru_failure(
                db,
                document=document,
                parse_run=parse_run,
                stage=stage,
                error=exc,
                settings=settings,
            )
        else:
            _mark_document_parse_failed(
                db,
                document.id,
                (
                    "MinerU 文档解析失败"
                    f"（阶段：{stage}，错误类型：{exc.__class__.__name__}）。"
                ),
            )
        raise _mineru_business_error(
            document_id=document.id,
            stage=stage,
            error=exc,
        ) from exc

    return DocumentParseResult(
        document_id=document.id,
        process_status=document.process_status,
        chunk_count=len(chunk_objects),
        parser_name=parse_result.parser_name,
        parser_version=parse_result.parser_version or "unknown",
    )


def list_document_chunks(
    db: Session,
    document_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> DocumentChunkListResult:
    _get_document_or_raise(db, document_id)

    total = db.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.document_id == document_id),
    ) or 0
    items = list(
        db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
            .limit(limit)
            .offset(offset),
        ).all(),
    )
    all_chunks = list(
        db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
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


def _mineru_chunker_config(settings: Any) -> BlockChunkerConfig:
    max_chunk_chars = max(1, int(settings.chunk_size_chars))
    overlap_chars = min(
        max(0, int(settings.chunk_overlap_chars)),
        max_chunk_chars - 1,
    )
    return BlockChunkerConfig(
        max_chunk_chars=max_chunk_chars,
        min_chunk_chars=min(200, max_chunk_chars),
        overlap_chars=overlap_chars,
        max_table_chars=max(4000, max_chunk_chars),
        keep_table_intact=True,
        keep_formula_with_context=True,
        include_headers_footers=False,
    )


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


def _add_mineru_chunks(
    db: Session,
    *,
    document_id: UUID,
    parse_run_id: UUID,
    chunks: list[BuiltDocumentChunk],
) -> list[DocumentChunk]:
    objects = [
        DocumentChunk(
            id=uuid4(),
            document_id=document_id,
            parse_run_id=parse_run_id,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            token_count=chunk.estimated_token_count,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            section_title=chunk.section_title,
            chunk_type=chunk.chunk_type,
            chunk_method=chunk.chunk_method,
            content_format=chunk.content_format,
            source_metadata=dict(chunk.source_metadata),
            embedding=None,
            embedding_model=None,
            embedding_dim=None,
            embedding_status="not_started",
        )
        for chunk in chunks
    ]
    if objects:
        db.add_all(objects)
        db.flush()
    return objects


def _add_chunk_block_mappings(
    db: Session,
    *,
    chunks: list[DocumentChunk],
    links: list[BuiltChunkBlockLink],
) -> list[DocumentChunkBlock]:
    chunk_ids = {chunk.chunk_index: chunk.id for chunk in chunks}
    mappings: list[DocumentChunkBlock] = []
    for link in links:
        chunk_id = chunk_ids.get(link.chunk_index)
        if chunk_id is None:
            raise ValueError("Chunk-block mapping references an unknown chunk")
        if link.block_id is None:
            raise ValueError("Chunk-block mapping requires a persisted block id")
        mappings.append(
            DocumentChunkBlock(
                id=uuid4(),
                chunk_id=chunk_id,
                block_id=UUID(link.block_id),
                block_order=link.block_order,
            )
        )
    if mappings:
        db.add_all(mappings)
        db.flush()
    return mappings


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
