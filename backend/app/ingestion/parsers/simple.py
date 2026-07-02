from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ingestion.parsers.base import ParsedDocument


TEXT_SOURCE_TYPES = {
    ".txt": "text",
    ".md": "markdown",
    ".csv": "csv",
}

PLACEHOLDER_SOURCE_TYPES = {
    ".pdf": "pdf",
    ".doc": "word",
    ".docx": "word",
    ".xls": "excel",
    ".xlsx": "excel",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".bmp": "image",
    ".tif": "image",
    ".tiff": "image",
    ".webp": "image",
}


@dataclass(frozen=True)
class SimpleParser:
    parser_name: str = "simple"
    parser_version: str = "0.1.0"

    def parse(
        self,
        *,
        content: bytes,
        filename: str,
        file_type: str,
        mime_type: str | None = None,
    ) -> ParsedDocument:
        extension = normalize_extension(filename=filename, file_type=file_type)

        if extension in TEXT_SOURCE_TYPES:
            return self._parse_text_content(
                content=content,
                filename=filename,
                extension=extension,
                mime_type=mime_type,
                source_type=TEXT_SOURCE_TYPES[extension],
            )

        return self._parse_placeholder(
            filename=filename,
            extension=extension,
            mime_type=mime_type,
            source_type=PLACEHOLDER_SOURCE_TYPES.get(extension, "unknown"),
        )

    def _base_metadata(
        self,
        *,
        filename: str,
        extension: str,
        mime_type: str | None,
        source_type: str,
        placeholder: bool,
    ) -> dict[str, Any]:
        return {
            "original_filename": filename,
            "original_extension": extension,
            "mime_type": mime_type,
            "source_type": source_type,
            "placeholder": placeholder,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
        }

    def _parse_text_content(
        self,
        *,
        content: bytes,
        filename: str,
        extension: str,
        mime_type: str | None,
        source_type: str,
    ) -> ParsedDocument:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("utf-8", errors="replace")

        metadata = self._base_metadata(
            filename=filename,
            extension=extension,
            mime_type=mime_type,
            source_type=source_type,
            placeholder=False,
        )

        return ParsedDocument(
            markdown=text,
            text=text,
            metadata=metadata,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            source_type=source_type,
        )

    def _parse_placeholder(
        self,
        *,
        filename: str,
        extension: str,
        mime_type: str | None,
        source_type: str,
    ) -> ParsedDocument:
        reason = "当前为轻量解析占位，后续由 MinerU 替换。"
        metadata = self._base_metadata(
            filename=filename,
            extension=extension,
            mime_type=mime_type,
            source_type=source_type,
            placeholder=True,
        )
        metadata["reason"] = reason

        text = "\n".join(
            [
                f"原始文件名：{filename}",
                f"文件类型：{extension or 'unknown'}",
                f"parser_name：{self.parser_name}",
                f"source_type：{source_type}",
                reason,
            ],
        )

        return ParsedDocument(
            markdown=text,
            text=text,
            metadata=metadata,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            source_type=source_type,
        )


def normalize_extension(*, filename: str, file_type: str) -> str:
    extension = (file_type or Path(filename).suffix).strip().lower()
    if extension and not extension.startswith("."):
        return f".{extension}"
    return extension
