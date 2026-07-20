from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class MinerUClientError(RuntimeError):
    """Base error for the internal MinerU client boundary."""


class MinerUConfigError(MinerUClientError):
    """Raised when required MinerU client configuration is invalid."""


class MinerUTimeoutError(MinerUClientError):
    """Raised when a MinerU task does not finish within the polling limit."""


class MinerURemoteError(MinerUClientError):
    """Raised when MinerU rejects a request or reports a failed task."""


_MODEL_VERSION_ALIASES = {
    "auto": "vlm",
    "vlm": "vlm",
    "pipeline": "pipeline",
    "mineru-html": "MinerU-HTML",
}


def normalize_mineru_model_version(parse_mode: str) -> str:
    normalized = parse_mode.strip().lower()
    try:
        return _MODEL_VERSION_ALIASES[normalized]
    except KeyError as exc:
        raise MinerUConfigError(
            "MinerU parse mode must be auto, vlm, pipeline, or MinerU-HTML"
        ) from exc


@dataclass(frozen=True, slots=True)
class MinerUParseRequest:
    filename: str
    content: bytes = field(repr=False)
    mime_type: str | None = None
    parse_mode: str = "auto"
    enable_ocr: bool = True
    save_intermediate: bool = True

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class MinerUResultFile:
    file_type: str
    filename: str
    content_type: str | None = None
    source_path: str | None = None
    download_url: str | None = field(default=None, repr=False)
    content: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MinerUAssetResult:
    asset_type: str
    asset_key: str
    filename: str
    mime_type: str | None = None
    page_number: int | None = None
    caption: str | None = None
    source_path: str | None = None
    download_url: str | None = field(default=None, repr=False)
    content: bytes | None = field(default=None, repr=False)
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class MinerUParseResult:
    parser_name: str
    parser_version: str | None
    parse_mode: str
    markdown_text: str = field(repr=False)
    text: str = field(repr=False)
    content_list: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)
    result_files: tuple[MinerUResultFile, ...] = field(default=(), repr=False)
    assets: tuple[MinerUAssetResult, ...] = field(default=(), repr=False)
    raw_metadata: Mapping[str, Any] = field(default_factory=dict)
    page_count: int | None = None


@runtime_checkable
class MinerUClientProtocol(Protocol):
    def parse_file(self, request: MinerUParseRequest) -> MinerUParseResult:
        """Parse one file and return the stable internal result model."""
        ...


@runtime_checkable
class MinerUTransportProtocol(Protocol):
    def submit(self, request: MinerUParseRequest) -> Mapping[str, Any]:
        """Submit a parse task without exposing an external route shape."""
        ...

    def get_task(self, task_id: str) -> Mapping[str, Any]:
        """Return the current external task state."""
        ...

    def get_result(
        self,
        task_id: str,
        task: Mapping[str, Any],
    ) -> MinerUParseResult:
        """Return the completed task as the stable internal result model."""
        ...
