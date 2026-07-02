from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ParsedDocument:
    markdown: str
    text: str
    metadata: dict[str, Any]
    parser_name: str
    parser_version: str
    source_type: str


class Parser(Protocol):
    parser_name: str
    parser_version: str

    def parse(
        self,
        *,
        content: bytes,
        filename: str,
        file_type: str,
        mime_type: str | None = None,
    ) -> ParsedDocument:
        ...
