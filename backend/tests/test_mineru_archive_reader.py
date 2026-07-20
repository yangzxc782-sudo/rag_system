from __future__ import annotations

import json
import stat
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from app.ingestion.mineru.archive_reader import MinerUArchiveReader
from app.ingestion.mineru.models import MinerURemoteError


def _zip_bytes(
    entries: dict[str, bytes],
    *,
    compression: int = ZIP_DEFLATED,
) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=compression) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _minimal_archive(*, markdown: bytes = b"# Casting") -> bytes:
    return _zip_bytes(
        {
            "result/full.md": markdown,
            "result/casting_content_list.json": b"[]",
        }
    )


def _content_list() -> list[dict[str, object]]:
    return [
        {
            "type": "text",
            "text": "Casting process",
            "text_level": 1,
            "page_idx": 0,
            "bbox": [10, 20, 900, 80],
        },
        {
            "type": "equation",
            "text": "$$Q = mc\\Delta T$$",
            "text_format": "latex",
            "img_path": "images/formula.png",
            "page_idx": 1,
            "bbox": [100, 200, 700, 300],
        },
        {
            "type": "table",
            "table_body": "<table><tr><td>A</td><td>B</td></tr></table>",
            "table_caption": ["Table 1"],
            "table_footnote": ["Casting values"],
            "img_path": "images/table.png",
            "page_idx": 2,
            "bbox": [50, 100, 950, 850],
        },
        {
            "type": "image",
            "img_path": "images/mould.png",
            "image_caption": ["Mould layout"],
            "page_idx": 2,
            "bbox": [100, 100, 500, 500],
        },
    ]


def _official_archive(*, include_intermediate: bool = True) -> bytes:
    stable_content = json.dumps(_content_list(), ensure_ascii=False).encode("utf-8")
    entries = {
        "result/full.md": b"# Casting process\n\nStructured result.",
        "result/casting_content_list.json": stable_content,
        "result/casting_content_list_v2.json": b'[[{"type":"title"}]]',
        "result/images/formula.png": b"formula-image",
        "result/images/table.png": b"table-image",
        "result/images/mould.png": b"mould-image",
    }
    if include_intermediate:
        entries.update(
            {
                "result/casting_middle.json": json.dumps(
                    {"_backend": "vlm", "_version_name": "3.0.0"}
                ).encode("utf-8"),
                "result/casting_model.json": b'[{"type":"text"}]',
            }
        )
    return _zip_bytes(entries)


def test_archive_reader_parses_nested_official_outputs_and_assets() -> None:
    result = MinerUArchiveReader().read(
        _official_archive(),
        batch_id="batch-test-1",
        filename="casting.pdf",
        parse_mode="vlm",
        save_intermediate=True,
    )

    assert result.parser_name == "mineru_api"
    assert result.parser_version == "3.0.0"
    assert result.parse_mode == "vlm"
    assert result.markdown_text.startswith("# Casting process")
    assert result.content_list == tuple(_content_list())
    assert result.page_count == 3
    assert [item.file_type for item in result.result_files] == [
        "markdown",
        "output_json",
        "layout_json",
        "model_json",
    ]
    assert [item.filename for item in result.result_files[:2]] == [
        "output.md",
        "output.json",
    ]
    assert result.result_files[0].source_path == "result/full.md"
    assert result.result_files[1].source_path.endswith("_content_list.json")
    assert {asset.asset_key for asset in result.assets} == {
        "images/formula.png",
        "images/table.png",
        "images/mould.png",
    }
    assert all(asset.content is not None for asset in result.assets)
    assert next(
        asset for asset in result.assets if asset.asset_key == "images/formula.png"
    ).asset_type == "formula_image"
    assert next(
        asset for asset in result.assets if asset.asset_key == "images/table.png"
    ).asset_type == "table_image"
    assert next(
        asset for asset in result.assets if asset.asset_key == "images/mould.png"
    ).caption == "Mould layout"
    assert result.raw_metadata == {
        "batch_id": "batch-test-1",
        "api_version": "v4",
        "final_status": "done",
        "parse_mode": "vlm",
        "file_count": 8,
        "asset_count": 3,
        "page_count": 3,
        "zip_file_types": [
            "content_list",
            "content_list_v2",
            "image",
            "markdown",
            "middle_json",
            "model_json",
        ],
    }
    assert not any(
        isinstance(value, (bytes, bytearray))
        for value in result.raw_metadata.values()
    )
    assert "http" not in repr(result.raw_metadata).lower()


def test_archive_reader_prefers_stable_content_list_over_v2() -> None:
    result = MinerUArchiveReader().read(
        _official_archive(),
        batch_id="batch-test-2",
        filename="casting.pdf",
        parse_mode="pipeline",
        save_intermediate=False,
    )

    assert result.content_list[0]["text"] == "Casting process"
    assert [item.file_type for item in result.result_files] == [
        "markdown",
        "output_json",
    ]


def test_archive_reader_preserves_formula_and_table_body_verbatim() -> None:
    result = MinerUArchiveReader().read(
        _official_archive(),
        batch_id="batch-test-3",
        filename="casting.pdf",
        parse_mode="vlm",
        save_intermediate=True,
    )

    assert result.content_list[1]["text"] == "$$Q = mc\\Delta T$$"
    assert result.content_list[2]["table_body"] == (
        "<table><tr><td>A</td><td>B</td></tr></table>"
    )


@pytest.mark.parametrize(
    ("archive_bytes", "message"),
    [
        (b"not-a-zip", "invalid ZIP"),
        (
            _zip_bytes(
                {
                    "result/casting_content_list.json": b"[]",
                }
            ),
            "full.md",
        ),
        (
            _zip_bytes({"result/full.md": b"# Missing content list"}),
            "content_list",
        ),
        (
            _zip_bytes(
                {
                    "result/full.md": b"# Invalid JSON",
                    "result/casting_content_list.json": b"not-json",
                }
            ),
            "content_list JSON",
        ),
    ],
)
def test_archive_reader_rejects_invalid_or_incomplete_archives(
    archive_bytes: bytes,
    message: str,
) -> None:
    with pytest.raises(MinerURemoteError, match=message):
        MinerUArchiveReader().read(
            archive_bytes,
            batch_id="batch-invalid",
            filename="casting.pdf",
            parse_mode="vlm",
            save_intermediate=True,
        )


def test_archive_reader_rejects_zip_path_traversal() -> None:
    archive_bytes = _zip_bytes(
        {
            "result/full.md": b"# Casting",
            "result/casting_content_list.json": b"[]",
            "../escape.txt": b"escape",
        }
    )

    with pytest.raises(MinerURemoteError, match="unsafe path"):
        MinerUArchiveReader().read(
            archive_bytes,
            batch_id="batch-unsafe",
            filename="casting.pdf",
            parse_mode="vlm",
            save_intermediate=True,
        )


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "/absolute.txt",
        "../escape.txt",
        "result/../../escape.txt",
        "C:/windows-drive.txt",
        "\\absolute.txt",
        "..\\escape.txt",
        "result/bad\x00name.txt",
    ],
)
def test_archive_reader_rejects_unsafe_member_metadata_paths(
    unsafe_name: str,
) -> None:
    archive_bytes = _minimal_archive()
    reader = MinerUArchiveReader()

    with ZipFile(BytesIO(archive_bytes)) as archive:
        archive.infolist()[0].filename = unsafe_name
        with pytest.raises(MinerURemoteError, match="unsafe path"):
            reader._validated_files(archive)


def test_archive_reader_rejects_symbolic_link_members() -> None:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("result/full.md", b"# Casting")
        archive.writestr("result/casting_content_list.json", b"[]")
        link = ZipInfo("result/images/link.png")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b"target")

    with pytest.raises(MinerURemoteError, match="unsafe path"):
        MinerUArchiveReader().read(
            buffer.getvalue(),
            batch_id="batch-symlink",
            filename="casting.pdf",
            parse_mode="vlm",
            save_intermediate=True,
        )


def test_archive_reader_rejects_encrypted_member_during_metadata_preflight() -> None:
    archive_bytes = _zip_bytes(
        {
            "result/full.md": b"# Casting",
            "result/casting_content_list.json": b"[]",
            "result/unread.bin": b"encrypted-placeholder",
        }
    )
    reader = MinerUArchiveReader()

    with ZipFile(BytesIO(archive_bytes)) as archive:
        archive.getinfo("result/unread.bin").flag_bits |= 0x1
        with pytest.raises(MinerURemoteError, match="encrypted"):
            reader._validated_files(archive)


@pytest.mark.parametrize("compression", [ZIP_STORED, ZIP_DEFLATED])
def test_archive_reader_accepts_supported_compression_algorithms(
    compression: int,
) -> None:
    result = MinerUArchiveReader().read(
        _zip_bytes(
            {
                "result/full.md": b"# Casting",
                "result/casting_content_list.json": b"[]",
            },
            compression=compression,
        ),
        batch_id="batch-supported-compression",
        filename="casting.pdf",
        parse_mode="vlm",
        save_intermediate=True,
    )

    assert result.markdown_text == "# Casting"


def test_archive_reader_rejects_unsupported_compression_during_preflight() -> None:
    archive_bytes = _minimal_archive()
    reader = MinerUArchiveReader()

    with ZipFile(BytesIO(archive_bytes)) as archive:
        archive.getinfo("result/full.md").compress_type = 99
        with pytest.raises(MinerURemoteError, match="compression algorithm"):
            reader._validated_files(archive)


def test_archive_reader_normalizes_safe_separators_and_dot_segments() -> None:
    archive_bytes = _zip_bytes(
        {
            "root//nested/./full.md": b"# Casting",
            "root//nested/./casting_content_list.json": b"[]",
        }
    )

    result = MinerUArchiveReader().read(
        archive_bytes,
        batch_id="batch-normalized-paths",
        filename="casting.pdf",
        parse_mode="vlm",
        save_intermediate=True,
    )

    assert result.result_files[0].source_path == "root/nested/full.md"
    assert result.result_files[1].source_path == (
        "root/nested/casting_content_list.json"
    )


def test_archive_reader_allows_member_count_at_limit() -> None:
    result = MinerUArchiveReader(max_member_count=2).read(
        _minimal_archive(),
        batch_id="batch-member-count-limit",
        filename="casting.pdf",
        parse_mode="vlm",
        save_intermediate=True,
    )

    assert result.markdown_text == "# Casting"


def test_archive_reader_rejects_member_count_over_limit() -> None:
    archive_bytes = _zip_bytes(
        {
            "result/full.md": b"# Casting",
            "result/casting_content_list.json": b"[]",
            "result/empty.txt": b"",
        }
    )

    with pytest.raises(MinerURemoteError, match="member count"):
        MinerUArchiveReader(max_member_count=2).read(
            archive_bytes,
            batch_id="batch-member-count-over",
            filename="casting.pdf",
            parse_mode="vlm",
            save_intermediate=True,
        )


def test_archive_reader_allows_member_size_at_limit() -> None:
    result = MinerUArchiveReader(max_member_bytes=4).read(
        _minimal_archive(markdown=b"1234"),
        batch_id="batch-member-size-limit",
        filename="casting.pdf",
        parse_mode="vlm",
        save_intermediate=True,
    )

    assert result.markdown_text == "1234"


def test_archive_reader_rejects_regular_member_over_size_limit() -> None:
    archive_bytes = _zip_bytes(
        {
            "result/full.md": b"1",
            "result/casting_content_list.json": b"[]",
            "result/oversized.bin": b"12345",
        }
    )

    with pytest.raises(MinerURemoteError, match="member exceeds"):
        MinerUArchiveReader(max_member_bytes=4).read(
            archive_bytes,
            batch_id="batch-member-size-over",
            filename="casting.pdf",
            parse_mode="vlm",
            save_intermediate=True,
        )


def test_archive_reader_allows_total_uncompressed_size_at_limit() -> None:
    result = MinerUArchiveReader(max_total_uncompressed_bytes=3).read(
        _minimal_archive(markdown=b"1"),
        batch_id="batch-total-size-limit",
        filename="casting.pdf",
        parse_mode="vlm",
        save_intermediate=True,
    )

    assert result.markdown_text == "1"


def test_archive_reader_rejects_total_uncompressed_size_over_limit() -> None:
    with pytest.raises(MinerURemoteError, match="uncompressed content"):
        MinerUArchiveReader(max_total_uncompressed_bytes=2).read(
            _minimal_archive(markdown=b"1"),
            batch_id="batch-total-size-over",
            filename="casting.pdf",
            parse_mode="vlm",
            save_intermediate=True,
        )


def test_archive_reader_allows_reasonable_compression_ratio() -> None:
    result = MinerUArchiveReader(max_compression_ratio=1000).read(
        _minimal_archive(markdown=b"A" * 4096),
        batch_id="batch-compression-ratio-ok",
        filename="casting.pdf",
        parse_mode="vlm",
        save_intermediate=True,
    )

    assert result.markdown_text == "A" * 4096


def test_archive_reader_allows_compression_ratio_exactly_at_limit() -> None:
    archive_bytes = _minimal_archive()
    reader = MinerUArchiveReader(max_compression_ratio=2)

    with ZipFile(BytesIO(archive_bytes)) as archive:
        info = archive.getinfo("result/full.md")
        info.file_size = 10
        info.compress_size = 5
        files = reader._validated_files(archive)

    assert info in files


def test_archive_reader_rejects_excessive_compression_ratio() -> None:
    with pytest.raises(MinerURemoteError, match="compression ratio"):
        MinerUArchiveReader(max_compression_ratio=2).read(
            _minimal_archive(markdown=b"A" * 4096),
            batch_id="batch-compression-ratio-over",
            filename="casting.pdf",
            parse_mode="vlm",
            save_intermediate=True,
        )


def test_archive_reader_rejects_nonempty_member_with_zero_compressed_size() -> None:
    archive_bytes = _minimal_archive()
    reader = MinerUArchiveReader()

    with ZipFile(BytesIO(archive_bytes)) as archive:
        info = archive.getinfo("result/full.md")
        info.compress_size = 0
        with pytest.raises(MinerURemoteError, match="compression ratio"):
            reader._validated_files(archive)


def test_archive_reader_allows_empty_member_with_zero_compressed_size() -> None:
    archive_bytes = _zip_bytes(
        {
            "result/full.md": b"# Casting",
            "result/casting_content_list.json": b"[]",
            "result/empty.txt": b"",
        }
    )

    result = MinerUArchiveReader().read(
        archive_bytes,
        batch_id="batch-empty-member",
        filename="casting.pdf",
        parse_mode="vlm",
        save_intermediate=True,
    )

    assert result.markdown_text == "# Casting"


def test_archive_reader_limit_error_does_not_expose_member_content() -> None:
    secret_content = "SENSITIVE-DOCUMENT-CONTENT"
    archive_bytes = _zip_bytes(
        {
            "result/full.md": b"# Casting",
            "result/casting_content_list.json": b"[]",
            "result/oversized.bin": secret_content.encode("utf-8"),
        }
    )

    with pytest.raises(MinerURemoteError) as exc_info:
        MinerUArchiveReader(max_member_bytes=4).read(
            archive_bytes,
            batch_id="batch-safe-error",
            filename="casting.pdf",
            parse_mode="vlm",
            save_intermediate=True,
        )

    assert secret_content not in str(exc_info.value)
    assert secret_content not in repr(exc_info.value)


def test_archive_reader_rejects_oversized_assets() -> None:
    archive_bytes = _zip_bytes(
        {
            "result/full.md": b"# Casting",
            "result/casting_content_list.json": b"[]",
            "result/images/large.png": b"12345",
        }
    )
    reader = MinerUArchiveReader(max_asset_bytes=4)

    with pytest.raises(MinerURemoteError, match="asset exceeds"):
        reader.read(
            archive_bytes,
            batch_id="batch-large-asset",
            filename="casting.pdf",
            parse_mode="vlm",
            save_intermediate=True,
        )


def test_archive_reader_does_not_treat_v2_as_stable_fallback() -> None:
    archive_bytes = _zip_bytes(
        {
            "result/full.md": b"# Casting",
            "result/casting_content_list_v2.json": b"[]",
        }
    )

    with pytest.raises(MinerURemoteError, match="stable content_list"):
        MinerUArchiveReader().read(
            archive_bytes,
            batch_id="batch-v2-only",
            filename="casting.pdf",
            parse_mode="vlm",
            save_intermediate=True,
        )
