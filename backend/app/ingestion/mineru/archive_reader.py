from __future__ import annotations

import json
import mimetypes
import re
import stat
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from app.ingestion.mineru.models import (
    MinerUAssetResult,
    MinerUParseResult,
    MinerURemoteError,
    MinerUResultFile,
)

DEFAULT_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_MEMBER_COUNT = 10_000
DEFAULT_MAX_MEMBER_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ASSET_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_COMPRESSION_RATIO = 200.0
_ZIP_ENCRYPTED_FLAG = 0x1
# MinerU V4 result archives only need the interoperable stored/deflated methods.
_SUPPORTED_COMPRESSION_TYPES = frozenset({ZIP_STORED, ZIP_DEFLATED})


class MinerUArchiveReader:
    """Convert one official V4 result ZIP into the stable internal result."""

    def __init__(
        self,
        *,
        max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
        max_member_count: int = DEFAULT_MAX_MEMBER_COUNT,
        max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
        max_total_uncompressed_bytes: int = DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES,
        max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES,
        max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
    ) -> None:
        for name, value in (
            ("max_archive_bytes", max_archive_bytes),
            ("max_member_count", max_member_count),
            ("max_member_bytes", max_member_bytes),
            ("max_total_uncompressed_bytes", max_total_uncompressed_bytes),
            ("max_asset_bytes", max_asset_bytes),
            ("max_compression_ratio", max_compression_ratio),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self._max_archive_bytes = max_archive_bytes
        self._max_member_count = max_member_count
        self._max_member_bytes = max_member_bytes
        self._max_total_uncompressed_bytes = max_total_uncompressed_bytes
        self._max_asset_bytes = max_asset_bytes
        self._max_compression_ratio = max_compression_ratio

    def read(
        self,
        archive_bytes: bytes,
        *,
        batch_id: str,
        filename: str,
        parse_mode: str,
        save_intermediate: bool,
    ) -> MinerUParseResult:
        if not archive_bytes or len(archive_bytes) > self._max_archive_bytes:
            raise MinerURemoteError(
                "MinerU result ZIP is empty or exceeds the configured size limit"
            )

        archive_error: MinerURemoteError | None = None
        files: list[ZipInfo] = []
        markdown_text = ""
        content_list: tuple[Mapping[str, Any], ...] = ()
        parser_version: str | None = None
        result_files: tuple[MinerUResultFile, ...] = ()
        assets: tuple[MinerUAssetResult, ...] = ()
        try:
            with ZipFile(BytesIO(archive_bytes)) as archive:
                files = self._validated_files(archive)
                markdown_info = _find_markdown(files)
                if markdown_info is None:
                    raise MinerURemoteError("MinerU result ZIP is missing full.md")

                content_info = _find_stable_content_list(files)
                if content_info is None:
                    if _find_content_list_v2(files) is not None:
                        raise MinerURemoteError(
                            "MinerU result ZIP is missing stable content_list.json"
                        )
                    raise MinerURemoteError(
                        "MinerU result ZIP is missing content_list.json"
                    )

                markdown_bytes = self._read_member(archive, markdown_info)
                content_bytes = self._read_member(archive, content_info)
                markdown_text = _decode_utf8(markdown_bytes, "full.md")
                content_list = _parse_content_list(content_bytes)

                middle_info = _find_middle(files)
                model_info = _find_model(files)
                parser_version = self._parser_version(archive, middle_info)
                result_files = self._result_files(
                    archive,
                    markdown_info=markdown_info,
                    markdown_bytes=markdown_bytes,
                    content_info=content_info,
                    content_bytes=content_bytes,
                    middle_info=middle_info,
                    model_info=model_info,
                    save_intermediate=save_intermediate,
                )
                assets = self._assets(archive, files, content_list)
        except MinerURemoteError:
            raise
        except BadZipFile:
            archive_error = MinerURemoteError(
                "MinerU returned an invalid ZIP archive"
            )
        except Exception:
            archive_error = MinerURemoteError(
                "MinerU result ZIP could not be processed"
            )

        if archive_error is not None:
            raise archive_error

        page_count = _page_count(content_list)
        raw_metadata = {
            "batch_id": batch_id,
            "api_version": "v4",
            "final_status": "done",
            "parse_mode": parse_mode,
            "file_count": len(files),
            "asset_count": len(assets),
            "page_count": page_count,
            "zip_file_types": _zip_file_types(files),
        }
        return MinerUParseResult(
            parser_name="mineru_api",
            parser_version=parser_version,
            parse_mode=parse_mode,
            markdown_text=markdown_text,
            text="",
            content_list=content_list,
            result_files=result_files,
            assets=assets,
            raw_metadata=raw_metadata,
            page_count=page_count,
        )

    def _validated_files(self, archive: ZipFile) -> list[ZipInfo]:
        members = archive.infolist()
        if len(members) > self._max_member_count:
            raise MinerURemoteError(
                "MinerU ZIP member count exceeds the configured limit"
            )

        files: list[ZipInfo] = []
        total_uncompressed = 0
        for info in members:
            _validate_archive_path(info)
            if info.flag_bits & _ZIP_ENCRYPTED_FLAG:
                raise MinerURemoteError("MinerU ZIP contains an encrypted member")
            if info.compress_type not in _SUPPORTED_COMPRESSION_TYPES:
                raise MinerURemoteError(
                    "MinerU ZIP uses an unsupported compression algorithm"
                )
            if info.is_dir():
                continue
            if info.file_size < 0 or info.compress_size < 0:
                raise MinerURemoteError("MinerU ZIP member metadata is invalid")
            if info.file_size > self._max_member_bytes:
                raise MinerURemoteError(
                    "MinerU ZIP member exceeds the configured size limit"
                )
            if info.file_size > 0:
                if info.compress_size == 0:
                    raise MinerURemoteError(
                        "MinerU ZIP member exceeds the compression ratio limit"
                    )
                compression_ratio = info.file_size / max(info.compress_size, 1)
                if compression_ratio > self._max_compression_ratio:
                    raise MinerURemoteError(
                        "MinerU ZIP member exceeds the compression ratio limit"
                    )
            total_uncompressed += info.file_size
            if total_uncompressed > self._max_total_uncompressed_bytes:
                raise MinerURemoteError(
                    "MinerU ZIP uncompressed content exceeds the configured size limit"
                )
            files.append(info)
        return files

    def _read_member(self, archive: ZipFile, info: ZipInfo) -> bytes:
        with archive.open(info) as stream:
            content = stream.read(self._max_member_bytes + 1)
        if len(content) > self._max_member_bytes:
            raise MinerURemoteError(
                "MinerU ZIP member exceeds the configured size limit"
            )
        return content

    def _read_asset(self, archive: ZipFile, info: ZipInfo) -> bytes:
        if info.file_size > self._max_asset_bytes:
            raise MinerURemoteError(
                "MinerU ZIP asset exceeds the configured size limit"
            )
        with archive.open(info) as stream:
            content = stream.read(self._max_asset_bytes + 1)
        if len(content) > self._max_asset_bytes:
            raise MinerURemoteError(
                "MinerU ZIP asset exceeds the configured size limit"
            )
        return content

    def _parser_version(
        self,
        archive: ZipFile,
        middle_info: ZipInfo | None,
    ) -> str | None:
        if middle_info is None:
            return None
        try:
            payload = json.loads(
                _decode_utf8(self._read_member(archive, middle_info), "middle.json")
            )
        except (MinerURemoteError, ValueError):
            return None
        if not isinstance(payload, Mapping):
            return None
        value = payload.get("_version_name") or payload.get("version")
        return _optional_text(value)

    def _result_files(
        self,
        archive: ZipFile,
        *,
        markdown_info: ZipInfo,
        markdown_bytes: bytes,
        content_info: ZipInfo,
        content_bytes: bytes,
        middle_info: ZipInfo | None,
        model_info: ZipInfo | None,
        save_intermediate: bool,
    ) -> tuple[MinerUResultFile, ...]:
        result_files = [
            MinerUResultFile(
                file_type="markdown",
                filename="output.md",
                content_type="text/markdown",
                source_path=_normalized_archive_path(markdown_info.filename),
                content=markdown_bytes,
            ),
            MinerUResultFile(
                file_type="output_json",
                filename="output.json",
                content_type="application/json",
                source_path=_normalized_archive_path(content_info.filename),
                content=content_bytes,
            ),
        ]
        if not save_intermediate:
            return tuple(result_files)

        for info, file_type in (
            (middle_info, "layout_json"),
            (model_info, "model_json"),
        ):
            if info is None:
                continue
            result_files.append(
                MinerUResultFile(
                    file_type=file_type,
                    filename=PurePosixPath(info.filename).name,
                    content_type="application/json",
                    source_path=_normalized_archive_path(info.filename),
                    content=self._read_member(archive, info),
                )
            )
        return tuple(result_files)

    def _assets(
        self,
        archive: ZipFile,
        files: Sequence[ZipInfo],
        content_list: tuple[Mapping[str, Any], ...],
    ) -> tuple[MinerUAssetResult, ...]:
        contexts = _asset_contexts(content_list)
        assets: list[MinerUAssetResult] = []
        for info in files:
            asset_key = _images_relative_path(info.filename)
            if asset_key is None:
                continue
            context = contexts.get(asset_key.casefold(), {})
            mime_type = mimetypes.guess_type(asset_key)[0]
            assets.append(
                MinerUAssetResult(
                    asset_type=str(context.get("asset_type") or "image"),
                    asset_key=asset_key,
                    filename=PurePosixPath(asset_key).name,
                    mime_type=mime_type,
                    page_number=_optional_int(context.get("page_number")),
                    caption=_optional_text(context.get("caption")),
                    source_path=_normalized_archive_path(info.filename),
                    content=self._read_asset(archive, info),
                    metadata={
                        "source_block_key": context.get("source_block_key"),
                    },
                )
            )
        return tuple(assets)


def _validate_archive_path(info: ZipInfo) -> None:
    name = info.filename.replace("\\", "/")
    path = PurePosixPath(name)
    mode = info.external_attr >> 16
    if (
        not name
        or name.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".."} for part in path.parts)
        or re.match(r"^[A-Za-z]:", name)
        or "\x00" in name
        or stat.S_ISLNK(mode)
    ):
        raise MinerURemoteError("MinerU ZIP contains an unsafe path")


def _find_markdown(files: Sequence[ZipInfo]) -> ZipInfo | None:
    return _pick(files, lambda name: name == "full.md")


def _find_stable_content_list(files: Sequence[ZipInfo]) -> ZipInfo | None:
    return _pick(
        files,
        lambda name: (
            name == "content_list.json"
            or name.endswith("_content_list.json")
        ),
    )


def _find_content_list_v2(files: Sequence[ZipInfo]) -> ZipInfo | None:
    return _pick(
        files,
        lambda name: (
            name == "content_list_v2.json"
            or name.endswith("_content_list_v2.json")
        ),
    )


def _find_middle(files: Sequence[ZipInfo]) -> ZipInfo | None:
    return _pick(
        files,
        lambda name: (
            name in {"middle.json", "layout.json"}
            or name.endswith("_middle.json")
        ),
    )


def _find_model(files: Sequence[ZipInfo]) -> ZipInfo | None:
    return _pick(
        files,
        lambda name: name == "model.json" or name.endswith("_model.json"),
    )


def _pick(files: Sequence[ZipInfo], predicate: Any) -> ZipInfo | None:
    matches = [
        info
        for info in files
        if predicate(PurePosixPath(info.filename).name.casefold())
    ]
    if not matches:
        return None
    return min(
        matches,
        key=lambda item: (
            len(PurePosixPath(item.filename).parts),
            item.filename,
        ),
    )


def _parse_content_list(content: bytes) -> tuple[Mapping[str, Any], ...]:
    parse_failed = False
    try:
        payload = json.loads(_decode_utf8(content, "content_list JSON"))
    except (ValueError, MinerURemoteError):
        parse_failed = True
        payload = None
    if parse_failed:
        raise MinerURemoteError("MinerU content_list JSON is invalid")
    if not isinstance(payload, list) or not all(
        isinstance(item, Mapping) for item in payload
    ):
        raise MinerURemoteError("MinerU content_list JSON has an invalid shape")
    return tuple(dict(item) for item in payload)


def _decode_utf8(content: bytes, label: str) -> str:
    decoded: str | None = None
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    if decoded is None:
        raise MinerURemoteError(f"MinerU {label} is not valid UTF-8")
    return decoded


def _asset_contexts(
    content_list: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(content_list):
        raw_path = _optional_text(item.get("img_path"))
        if raw_path is None:
            continue
        asset_key = _images_relative_path(raw_path)
        if asset_key is None:
            continue
        original_type = str(item.get("type") or "unknown").strip().lower()
        asset_type = {
            "equation": "formula_image",
            "table": "table_image",
        }.get(original_type, "image")
        contexts.setdefault(
            asset_key.casefold(),
            {
                "asset_type": asset_type,
                "page_number": item.get("page_idx"),
                "caption": _caption_for_item(item, original_type),
                "source_block_key": (
                    _optional_text(
                        item.get("block_key")
                        or item.get("block_id")
                        or item.get("id")
                    )
                    or f"block-{index:06d}"
                ),
            },
        )
    return contexts


def _caption_for_item(item: Mapping[str, Any], original_type: str) -> str | None:
    keys = {
        "image": ("image_caption", "image_footnote"),
        "chart": ("chart_caption", "chart_footnote"),
        "table": ("table_caption", "table_footnote"),
    }.get(original_type, ())
    values = [
        text
        for key in keys
        for text in _string_values(item.get(key))
    ]
    return "\n".join(values) or None


def _images_relative_path(value: str) -> str | None:
    normalized = _normalized_archive_path(value)
    parts = list(PurePosixPath(normalized).parts)
    for index, part in enumerate(parts):
        if part.casefold() == "images":
            relative_parts = parts[index:]
            relative_parts[0] = "images"
            return "/".join(relative_parts)
    return None


def _normalized_archive_path(value: str) -> str:
    return str(PurePosixPath(value.replace("\\", "/")))


def _page_count(content_list: Sequence[Mapping[str, Any]]) -> int | None:
    pages = [
        page
        for item in content_list
        if (page := _optional_int(item.get("page_idx"))) is not None and page >= 0
    ]
    return max(pages) + 1 if pages else None


def _zip_file_types(files: Sequence[ZipInfo]) -> list[str]:
    file_types: set[str] = set()
    for info in files:
        name = PurePosixPath(info.filename).name.casefold()
        if name == "full.md":
            file_types.add("markdown")
        elif name == "content_list_v2.json" or name.endswith("_content_list_v2.json"):
            file_types.add("content_list_v2")
        elif name == "content_list.json" or name.endswith("_content_list.json"):
            file_types.add("content_list")
        elif name in {"middle.json", "layout.json"} or name.endswith("_middle.json"):
            file_types.add("middle_json")
        elif name == "model.json" or name.endswith("_model.json"):
            file_types.add("model_json")
        elif _images_relative_path(info.filename) is not None:
            file_types.add("image")
        else:
            file_types.add("other")
    return sorted(file_types)


def _string_values(value: Any) -> list[str]:
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
