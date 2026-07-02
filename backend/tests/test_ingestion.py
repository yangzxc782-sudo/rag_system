from __future__ import annotations

import pytest

from app.core.errors import DOCUMENT_CHUNK_CONFIG_INVALID, DOCUMENT_PARSER_UNAVAILABLE, BusinessError
from app.ingestion.chunker import chunk_parsed_document
from app.ingestion.parsers.mineru import MinerUParser
from app.ingestion.parsers.simple import SimpleParser


def test_simple_parser_parses_txt() -> None:
    parsed = SimpleParser().parse(
        content="铸型工艺文本".encode("utf-8"),
        filename="notes.txt",
        file_type=".txt",
        mime_type="text/plain",
    )

    assert parsed.text == "铸型工艺文本"
    assert parsed.markdown == "铸型工艺文本"
    assert parsed.metadata["placeholder"] is False
    assert parsed.parser_name == "simple"
    assert parsed.source_type == "text"


def test_simple_parser_parses_markdown() -> None:
    parsed = SimpleParser().parse(
        content=b"# Title\n\nCasting process",
        filename="process.md",
        file_type=".md",
        mime_type="text/markdown",
    )

    assert "# Title" in parsed.markdown
    assert "Casting process" in parsed.text
    assert parsed.metadata["original_extension"] == ".md"
    assert parsed.metadata["original_filename"] == "process.md"


def test_simple_parser_parses_csv_without_extra_dependencies() -> None:
    parsed = SimpleParser().parse(
        content=b"name,value\nsand,42\n",
        filename="table.csv",
        file_type=".csv",
        mime_type="text/csv",
    )

    assert parsed.text
    assert "sand,42" in parsed.text
    assert parsed.source_type == "csv"


@pytest.mark.parametrize(
    ("filename", "file_type", "expected_source_type"),
    [
        ("manual.pdf", ".pdf", "pdf"),
        ("image.png", ".png", "image"),
    ],
)
def test_simple_parser_complex_formats_return_placeholder(
    filename: str,
    file_type: str,
    expected_source_type: str,
) -> None:
    parsed = SimpleParser().parse(
        content=b"binary content",
        filename=filename,
        file_type=file_type,
        mime_type="application/octet-stream",
    )

    assert filename in parsed.text
    assert file_type in parsed.text
    assert "parser_name" in parsed.text
    assert "source_type" in parsed.text
    assert "当前为轻量解析占位" in parsed.text
    assert "后续由 MinerU 替换" in parsed.text
    assert parsed.metadata["placeholder"] is True
    assert parsed.metadata["parser_name"] == "simple"
    assert parsed.metadata["parser_version"] == "0.1.0"
    assert parsed.metadata["source_type"] == expected_source_type
    assert parsed.metadata["original_extension"] == file_type
    assert parsed.metadata["original_filename"] == filename
    assert "reason" in parsed.metadata


def test_mineru_parser_is_unavailable_boundary() -> None:
    parser = MinerUParser(endpoint="http://127.0.0.1:9009", timeout_seconds=10)

    with pytest.raises(BusinessError) as exc_info:
        parser.parse(
            content=b"%PDF",
            filename="manual.pdf",
            file_type=".pdf",
            mime_type="application/pdf",
        )

    assert exc_info.value.code == DOCUMENT_PARSER_UNAVAILABLE


def test_chunker_splits_text_with_overlap_and_metadata() -> None:
    parsed = SimpleParser().parse(
        content=("a" * 1200).encode("utf-8"),
        filename="long.txt",
        file_type=".txt",
        mime_type="text/plain",
    )

    chunks = chunk_parsed_document(parsed, chunk_size_chars=1000, chunk_overlap_chars=100)

    assert [chunk.chunk_index for chunk in chunks] == [0, 1]
    assert len(chunks[0].content) == 1000
    assert chunks[0].chunk_type == "text"
    assert chunks[0].source_metadata["char_start"] == 0
    assert chunks[0].source_metadata["char_end"] == 1000
    assert chunks[0].source_metadata["character_count"] == 1000
    assert chunks[0].source_metadata["parser_name"] == "simple"
    assert chunks[0].source_metadata["parser_version"] == "0.1.0"
    assert chunks[0].source_metadata["source_type"] == "text"
    assert chunks[0].source_metadata["placeholder"] is False
    assert chunks[0].source_metadata["original_extension"] == ".txt"
    assert chunks[1].source_metadata["char_start"] == 900


def test_chunker_chunks_placeholder_text() -> None:
    parsed = SimpleParser().parse(
        content=b"%PDF",
        filename="manual.pdf",
        file_type=".pdf",
        mime_type="application/pdf",
    )

    chunks = chunk_parsed_document(parsed, chunk_size_chars=1000, chunk_overlap_chars=100)

    assert len(chunks) >= 1
    assert chunks[0].chunk_type == "placeholder"
    assert chunks[0].source_metadata["placeholder"] is True


@pytest.mark.parametrize(
    ("chunk_size_chars", "chunk_overlap_chars"),
    [
        (0, 0),
        (-1, 0),
        (100, -1),
        (100, 100),
        (100, 101),
    ],
)
def test_chunker_rejects_invalid_config(chunk_size_chars: int, chunk_overlap_chars: int) -> None:
    parsed = SimpleParser().parse(
        content=b"content",
        filename="notes.txt",
        file_type=".txt",
        mime_type="text/plain",
    )

    with pytest.raises(BusinessError) as exc_info:
        chunk_parsed_document(
            parsed,
            chunk_size_chars=chunk_size_chars,
            chunk_overlap_chars=chunk_overlap_chars,
        )

    assert exc_info.value.code == DOCUMENT_CHUNK_CONFIG_INVALID


def test_chunker_empty_or_blank_text_returns_empty_list() -> None:
    parsed = SimpleParser().parse(
        content=b"   \n\t  ",
        filename="blank.txt",
        file_type=".txt",
        mime_type="text/plain",
    )

    assert chunk_parsed_document(parsed, chunk_size_chars=1000, chunk_overlap_chars=100) == []
