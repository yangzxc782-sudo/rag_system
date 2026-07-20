from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from app.core.errors import DOCUMENT_PARSE_FAILED
from app.ingestion.mineru.models import (
    MinerUAssetResult,
    MinerUParseResult,
    MinerUResultFile,
)

SUPPORTED_BLOCK_TYPES = frozenset(
    {
        "title",
        "text",
        "list",
        "table",
        "formula",
        "image",
        "caption",
        "footnote",
        "header",
        "footer",
        "unknown",
    }
)
SUPPORTED_ASSET_TYPES = frozenset(
    {
        "image",
        "table_image",
        "formula_image",
        "page_image",
        "markdown",
        "json",
        "layout_json",
        "other",
    }
)

_BLOCK_TYPE_ALIASES = {
    "algorithm": "text",
    "aside_text": "footnote",
    "chart": "image",
    "code": "text",
    "heading": "title",
    "paragraph": "text",
    "plain_text": "text",
    "ref_text": "text",
    "list_item": "list",
    "table_body": "table",
    "equation": "formula",
    "interline_equation": "formula",
    "figure": "image",
    "image_caption": "caption",
    "page_footnote": "footnote",
    "page_number": "footer",
}
_SAFE_METADATA_KEYS = frozenset(
    {
        "api_version",
        "asset_count",
        "batch_id",
        "file_count",
        "final_status",
        "job_id",
        "model_version",
        "page_count",
        "task_id",
    }
)
_SAFE_BLOCK_METADATA_KEYS = frozenset(
    {
        "category_id",
        "confidence",
        "language",
        "model_name",
        "rotation",
        "sub_type",
        "text_format",
    }
)
_SAFE_ASSET_METADATA_KEYS = frozenset(
    {
        "checksum",
        "height",
        "width",
    }
)


class MinerUNormalizationError(RuntimeError):
    code = DOCUMENT_PARSE_FAILED


@dataclass(slots=True)
class NormalizedDocumentBlock:
    block_index: int
    block_key: str | None
    block_type: str
    page_start: int | None = None
    page_end: int | None = None
    bbox: dict[str, Any] | list[Any] | None = None
    text: str | None = field(default=None, repr=False)
    markdown: str | None = field(default=None, repr=False)
    html: str | None = field(default=None, repr=False)
    latex: str | None = field(default=None, repr=False)
    caption: str | None = field(default=None, repr=False)
    asset_keys: list[str] = field(default_factory=list)
    parent_block_key: str | None = None
    section_path: list[str] = field(default_factory=list)
    confidence: float | None = None
    source_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedDocumentAsset:
    asset_type: str
    asset_key: str
    filename: str
    page_number: int | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    caption: str | None = field(default=None, repr=False)
    source_block_key: str | None = None
    source_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedMinerUResult:
    blocks: list[NormalizedDocumentBlock]
    assets: list[NormalizedDocumentAsset]
    output_markdown_key: str
    output_json_key: str
    page_count: int | None
    block_count: int
    asset_count: int
    source_metadata: dict[str, Any] = field(default_factory=dict)


def normalize_mineru_result(
    result: MinerUParseResult,
    *,
    document_id: str,
    parse_run_id: str,
    output_prefix: str = "parsed-assets",
) -> NormalizedMinerUResult:
    prefix = _build_output_prefix(output_prefix, document_id, parse_run_id)
    blocks = _normalize_blocks(result, prefix)
    assets = _normalize_assets(result, blocks, prefix)
    page_count = result.page_count or _max_page(blocks)
    return NormalizedMinerUResult(
        blocks=blocks,
        assets=assets,
        output_markdown_key=f"{prefix}/output.md",
        output_json_key=f"{prefix}/output.json",
        page_count=page_count,
        block_count=len(blocks),
        asset_count=len(assets),
        source_metadata=_result_metadata_summary(result),
    )


def _normalize_blocks(
    result: MinerUParseResult,
    prefix: str,
) -> list[NormalizedDocumentBlock]:
    raw_blocks: Sequence[Any] = result.content_list
    if not raw_blocks and (result.text or result.markdown_text):
        raw_blocks = (
            {
                "type": "text",
                "text": result.text,
                "markdown": result.markdown_text,
            },
        )

    blocks: list[NormalizedDocumentBlock] = []
    section_path: list[str] = []
    for block_index, raw in enumerate(raw_blocks):
        try:
            if not isinstance(raw, Mapping):
                raise TypeError("block must be a mapping")
            block = _normalize_block(raw, block_index, section_path, prefix)
        except Exception as exc:
            raise MinerUNormalizationError(
                f"Failed to normalize MinerU block at index {block_index}"
            ) from exc
        blocks.append(block)
        if block.block_type == "title" and block.text:
            section_path = list(block.section_path)
    return blocks


def _normalize_block(
    raw: Mapping[str, Any],
    block_index: int,
    current_section_path: list[str],
    prefix: str,
) -> NormalizedDocumentBlock:
    original_type = _optional_text(
        raw.get("type") or raw.get("block_type") or raw.get("category")
    )
    block_type = _normalize_block_type(original_type)
    heading_level = _heading_level(raw)
    if (
        (original_type or "").strip().lower() == "text"
        and _optional_int(raw.get("text_level")) not in {None, 0}
    ):
        block_type = "title"
    text = _block_text(raw, original_type)
    explicit_section_path = _string_list(raw.get("section_path"))

    if block_type == "title" and text:
        next_section_path = list(current_section_path[: heading_level - 1])
        next_section_path.append(text)
        section_path = next_section_path
    else:
        section_path = explicit_section_path or list(current_section_path)

    page_start = _optional_int(
        raw.get("page_start")
        if raw.get("page_start") is not None
        else raw.get("page_number", raw.get("page", raw.get("page_idx")))
    )
    page_end = _optional_int(raw.get("page_end"))
    if page_end is None:
        page_end = page_start

    raw_asset_keys = _asset_references(raw)
    asset_keys = [
        _build_asset_key(prefix, value, fallback=f"block-{block_index:06d}")
        for value in raw_asset_keys
    ]
    block_key = _optional_text(
        raw.get("block_key") or raw.get("block_id") or raw.get("id")
    ) or f"block-{block_index:06d}"
    confidence_value = raw.get("confidence")
    if confidence_value is None:
        confidence_value = raw.get("score")
    confidence = _optional_float(confidence_value)

    source_metadata = {
        "original_type": _short_metadata_text(original_type) or "unknown",
        "mineru_block_id": _short_metadata_text(
            _optional_text(
                raw.get("block_id") or raw.get("id") or raw.get("block_key")
            )
        ),
        "heading_level": heading_level if block_type == "title" else None,
        **{
            key: _metadata_scalar(raw[key])
            for key in _SAFE_BLOCK_METADATA_KEYS
            if key in raw and _metadata_scalar(raw[key]) is not None
        },
    }
    source_metadata = {
        key: value for key, value in source_metadata.items() if value is not None
    }
    if asset_keys:
        source_metadata["asset_keys"] = list(asset_keys)

    markdown = _block_markdown(raw, block_type)

    return NormalizedDocumentBlock(
        block_index=block_index,
        block_key=block_key,
        block_type=block_type,
        page_start=page_start,
        page_end=page_end,
        bbox=_json_container(raw.get("bbox")),
        text=text,
        markdown=markdown,
        html=_block_html(raw, block_type),
        latex=_block_latex(raw, block_type),
        caption=_block_caption(raw, original_type),
        asset_keys=asset_keys,
        parent_block_key=_optional_text(
            raw.get("parent_block_key")
            or raw.get("parent_block_id")
            or raw.get("parent_id")
        ),
        section_path=section_path,
        confidence=confidence,
        source_metadata=source_metadata,
    )


def _normalize_assets(
    result: MinerUParseResult,
    blocks: Sequence[NormalizedDocumentBlock],
    prefix: str,
) -> list[NormalizedDocumentAsset]:
    assets: list[NormalizedDocumentAsset] = []
    known_keys: set[str] = set()

    for asset_index, raw_asset in enumerate(result.assets):
        try:
            asset = _normalize_asset(raw_asset, prefix, asset_index)
        except Exception as exc:
            raise MinerUNormalizationError(
                f"Failed to normalize MinerU asset at index {asset_index}"
            ) from exc
        _append_asset(assets, known_keys, asset)

    block_by_asset_key = {
        asset_key: block
        for block in blocks
        for asset_key in block.asset_keys
    }
    for asset_key, block in block_by_asset_key.items():
        _append_asset(
            assets,
            known_keys,
            NormalizedDocumentAsset(
                asset_type=_block_asset_type(block.block_type),
                asset_key=asset_key,
                filename=PurePosixPath(asset_key).name,
                page_number=block.page_start,
                caption=block.caption,
                source_block_key=block.block_key,
                source_metadata={"origin": "block_reference"},
            ),
        )

    for file_index, result_file in enumerate(result.result_files):
        try:
            asset = _normalize_result_file(result_file, prefix, file_index)
        except Exception as exc:
            raise MinerUNormalizationError(
                f"Failed to normalize MinerU result file at index {file_index}"
            ) from exc
        _append_asset(assets, known_keys, asset)
    return assets


def _normalize_asset(
    asset: MinerUAssetResult,
    prefix: str,
    asset_index: int,
) -> NormalizedDocumentAsset:
    filename = _safe_filename(asset.filename, f"asset-{asset_index:06d}")
    asset_key = _build_asset_key(prefix, asset.asset_key, fallback=filename)
    source_block_key = _optional_text(
        asset.metadata.get("source_block_key")
        or asset.metadata.get("block_key")
    )
    return NormalizedDocumentAsset(
        asset_type=_normalize_asset_type(asset.asset_type),
        asset_key=asset_key,
        filename=filename,
        page_number=asset.page_number,
        mime_type=asset.mime_type,
        size_bytes=len(asset.content) if asset.content is not None else None,
        caption=asset.caption,
        source_block_key=source_block_key,
        source_metadata={
            key: value
            for key, value in {
                **{
                    key: _metadata_scalar(asset.metadata[key])
                    for key in _SAFE_ASSET_METADATA_KEYS
                    if key in asset.metadata
                    and _metadata_scalar(asset.metadata[key]) is not None
                },
                "source_path": _short_metadata_text(asset.source_path),
            }.items()
            if value is not None
        },
    )


def _normalize_result_file(
    result_file: MinerUResultFile,
    prefix: str,
    file_index: int,
) -> NormalizedDocumentAsset:
    filename = _safe_filename(
        result_file.filename,
        f"result-{file_index:06d}",
    )
    return NormalizedDocumentAsset(
        asset_type=_normalize_asset_type(result_file.file_type),
        asset_key=_build_asset_key(prefix, filename, fallback=filename),
        filename=filename,
        mime_type=result_file.content_type,
        size_bytes=(
            len(result_file.content) if result_file.content is not None else None
        ),
        source_metadata={
            "origin": "result_file",
            "result_file_type": _normalize_asset_type(result_file.file_type),
            **(
                {"source_path": _short_metadata_text(result_file.source_path)}
                if result_file.source_path
                else {}
            ),
        },
    )


def _result_metadata_summary(result: MinerUParseResult) -> dict[str, Any]:
    summary = {
        "parser_name": result.parser_name,
        "parser_version": result.parser_version,
        "parse_mode": result.parse_mode,
    }
    summary.update(
        {
            key: _metadata_scalar(result.raw_metadata[key])
            for key in _SAFE_METADATA_KEYS
            if key in result.raw_metadata
            and _metadata_scalar(result.raw_metadata[key]) is not None
        }
    )
    zip_file_types = result.raw_metadata.get("zip_file_types")
    if isinstance(zip_file_types, (list, tuple)):
        summary["zip_file_types"] = [
            text[:100]
            for item in zip_file_types[:20]
            if (text := _optional_text(item))
        ]
    return {key: value for key, value in summary.items() if value is not None}


def _build_output_prefix(
    output_prefix: str,
    document_id: str,
    parse_run_id: str,
) -> str:
    prefix = _safe_relative_path(output_prefix, fallback="parsed-assets")
    document_segment = _safe_path_segment(document_id)
    parse_run_segment = _safe_path_segment(parse_run_id)
    if not document_segment or not parse_run_segment:
        raise MinerUNormalizationError("Document and parse run identifiers are required")
    return f"{prefix}/{document_segment}/{parse_run_segment}"


def _build_asset_key(prefix: str, value: str, *, fallback: str) -> str:
    relative = _safe_relative_path(value, fallback=fallback)
    if relative == prefix or relative.startswith(f"{prefix}/"):
        return relative
    return f"{prefix}/{relative}"


def _safe_relative_path(value: str, *, fallback: str) -> str:
    parts = [
        _safe_path_segment(part)
        for part in str(value).replace("\\", "/").split("/")
        if part not in {"", ".", ".."}
    ]
    safe_parts = [part for part in parts if part]
    return "/".join(safe_parts) or _safe_path_segment(fallback) or "asset"


def _safe_path_segment(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip()).strip("._")


def _safe_filename(value: str, fallback: str) -> str:
    filename = PurePosixPath(str(value).replace("\\", "/")).name
    return _safe_path_segment(filename) or fallback


def _normalize_block_type(value: str | None) -> str:
    normalized = (value or "unknown").strip().lower()
    normalized = _BLOCK_TYPE_ALIASES.get(normalized, normalized)
    return normalized if normalized in SUPPORTED_BLOCK_TYPES else "unknown"


def _normalize_asset_type(value: str | None) -> str:
    normalized = (value or "other").strip().lower()
    aliases = {
        "md": "markdown",
        "output_markdown": "markdown",
        "output_json": "json",
        "layout": "layout_json",
        "figure": "image",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in SUPPORTED_ASSET_TYPES else "other"


def _block_text(
    raw: Mapping[str, Any],
    original_type: str | None,
) -> str | None:
    normalized_type = (original_type or "unknown").strip().lower()
    explicit_text = _optional_text(raw.get("text"))
    content = _optional_text(raw.get("content"))
    if normalized_type == "list":
        values = _text_values(raw.get("list_items"))
        return "\n".join(values) or explicit_text or content
    if normalized_type in {"code", "algorithm"}:
        return _optional_text(raw.get("code_body")) or explicit_text or content
    if normalized_type == "equation":
        has_explicit_latex = any(
            _optional_text(raw.get(key))
            for key in ("latex", "formula", "equation")
        )
        if (
            not has_explicit_latex
            and (_optional_text(raw.get("text_format")) or "").lower() == "latex"
        ):
            return None
    return explicit_text or content


def _block_markdown(
    raw: Mapping[str, Any],
    block_type: str,
) -> str | None:
    markdown = _optional_text(raw.get("markdown"))
    if block_type != "table":
        return markdown
    table_markdown = _optional_text(raw.get("table_markdown"))
    table_body = _optional_text(raw.get("table_body"))
    if table_body and not _looks_like_html(table_body):
        return markdown or table_markdown or table_body
    return markdown or table_markdown


def _block_html(
    raw: Mapping[str, Any],
    block_type: str,
) -> str | None:
    html = _optional_text(raw.get("html"))
    if html or block_type != "table":
        return html
    table_body = _optional_text(raw.get("table_body"))
    return table_body if table_body and _looks_like_html(table_body) else None


def _block_latex(
    raw: Mapping[str, Any],
    block_type: str,
) -> str | None:
    explicit = _optional_text(
        raw.get("latex") or raw.get("formula") or raw.get("equation")
    )
    if explicit or block_type != "formula":
        return explicit
    if (_optional_text(raw.get("text_format")) or "").lower() == "latex":
        return _optional_text(raw.get("text") or raw.get("content"))
    return None


def _block_caption(
    raw: Mapping[str, Any],
    original_type: str | None,
) -> str | None:
    normalized_type = (original_type or "unknown").strip().lower()
    keys = ["caption"]
    keys.extend(
        {
            "image": ["image_caption", "image_footnote"],
            "figure": ["image_caption", "image_footnote"],
            "chart": ["chart_caption", "chart_footnote"],
            "table": ["table_caption", "table_footnote"],
            "code": ["code_caption", "code_footnote"],
            "algorithm": ["code_caption", "code_footnote"],
        }.get(normalized_type, [])
    )
    values = [
        text
        for key in keys
        for text in _text_values(raw.get(key))
    ]
    return "\n".join(dict.fromkeys(values)) or None


def _looks_like_html(value: str) -> bool:
    return value.lstrip().lower().startswith(("<table", "<html", "<body"))


def _heading_level(raw: Mapping[str, Any]) -> int:
    level = _optional_int(
        raw.get("level")
        or raw.get("heading_level")
        or raw.get("text_level")
    ) or 1
    return min(max(level, 1), 6)


def _asset_references(raw: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("asset_key", "image_path", "image_key", "img_path"):
        value = _optional_text(raw.get(key))
        if value:
            values.append(value)
    raw_assets = raw.get("asset_keys") or raw.get("assets")
    if isinstance(raw_assets, (list, tuple)):
        values.extend(
            value
            for item in raw_assets
            if (value := _optional_text(item))
        )
    return list(dict.fromkeys(values))


def _block_asset_type(block_type: str) -> str:
    if block_type == "table":
        return "table_image"
    if block_type == "formula":
        return "formula_image"
    return "image"


def _append_asset(
    assets: list[NormalizedDocumentAsset],
    known_keys: set[str],
    asset: NormalizedDocumentAsset,
) -> None:
    if asset.asset_key in known_keys:
        return
    known_keys.add(asset.asset_key)
    assets.append(asset)


def _max_page(blocks: Sequence[NormalizedDocumentBlock]) -> int | None:
    pages = [
        page
        for block in blocks
        for page in (block.page_start, block.page_end)
        if page is not None
    ]
    return max(pages) if pages else None


def _json_container(value: Any) -> dict[str, Any] | list[Any] | None:
    normalized = _json_compatible(value)
    return normalized if isinstance(normalized, (dict, list)) else None


def _json_compatible(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): normalized
            for key, item in value.items()
            if (normalized := _json_compatible(item)) is not None
        }
    if isinstance(value, (list, tuple)):
        return [
            normalized
            for item in value
            if (normalized := _json_compatible(item)) is not None
        ]
    return None


def _metadata_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, str):
        return _short_metadata_text(value)
    if isinstance(value, (int, float, bool)):
        return value
    return None


def _short_metadata_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text[:200] or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [text for item in value if (text := _optional_text(item))]


def _text_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [text for item in value if (text := _optional_text(item))]
    text = _optional_text(value)
    return [text] if text else []


def _optional_text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, bytes, bytearray)):
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
