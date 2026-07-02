from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.errors import DOCUMENT_CHUNK_CONFIG_INVALID, BusinessError
from app.ingestion.parsers.base import ParsedDocument


@dataclass(frozen=True)
class ParsedChunk:
    chunk_index: int
    content: str
    chunk_type: str
    source_metadata: dict[str, Any]


def validate_chunk_config(*, chunk_size_chars: int, chunk_overlap_chars: int) -> None:
    if chunk_size_chars <= 0:
        raise BusinessError(
            DOCUMENT_CHUNK_CONFIG_INVALID,
            "chunk_size_chars 必须大于 0。",
            detail={"chunk_size_chars": chunk_size_chars},
            status_code=400,
        )

    if chunk_overlap_chars < 0:
        raise BusinessError(
            DOCUMENT_CHUNK_CONFIG_INVALID,
            "chunk_overlap_chars 必须大于或等于 0。",
            detail={"chunk_overlap_chars": chunk_overlap_chars},
            status_code=400,
        )

    if chunk_overlap_chars >= chunk_size_chars:
        raise BusinessError(
            DOCUMENT_CHUNK_CONFIG_INVALID,
            "chunk_overlap_chars 必须小于 chunk_size_chars。",
            detail={
                "chunk_size_chars": chunk_size_chars,
                "chunk_overlap_chars": chunk_overlap_chars,
            },
            status_code=400,
        )


def chunk_parsed_document(
    parsed_document: ParsedDocument,
    *,
    chunk_size_chars: int,
    chunk_overlap_chars: int,
) -> list[ParsedChunk]:
    validate_chunk_config(
        chunk_size_chars=chunk_size_chars,
        chunk_overlap_chars=chunk_overlap_chars,
    )

    content = parsed_document.markdown or parsed_document.text
    if not content.strip():
        return []

    chunks: list[ParsedChunk] = []
    step_size = chunk_size_chars - chunk_overlap_chars
    start = 0

    while start < len(content):
        end = min(start + chunk_size_chars, len(content))
        chunk_content = content[start:end]
        placeholder = bool(parsed_document.metadata.get("placeholder", False))

        chunks.append(
            ParsedChunk(
                chunk_index=len(chunks),
                content=chunk_content,
                chunk_type="placeholder" if placeholder else "text",
                source_metadata={
                    "parser_name": parsed_document.parser_name,
                    "parser_version": parsed_document.parser_version,
                    "source_type": parsed_document.source_type,
                    "placeholder": placeholder,
                    "char_start": start,
                    "char_end": end,
                    "character_count": len(chunk_content),
                    "original_extension": parsed_document.metadata.get("original_extension"),
                    "original_filename": parsed_document.metadata.get("original_filename"),
                },
            ),
        )

        if end >= len(content):
            break

        start += step_size

    return chunks
