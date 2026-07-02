from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import DOCUMENT_PARSER_UNAVAILABLE, BusinessError
from app.ingestion.parsers.base import ParsedDocument


@dataclass(frozen=True)
class MinerUParser:
    endpoint: str = ""
    timeout_seconds: int = 60
    parser_name: str = "mineru"
    parser_version: str = "reserved-0.1.0"

    def parse(
        self,
        *,
        content: bytes,
        filename: str,
        file_type: str,
        mime_type: str | None = None,
    ) -> ParsedDocument:
        raise BusinessError(
            DOCUMENT_PARSER_UNAVAILABLE,
            "MinerU 解析器尚未接入，第三阶段仅预留调用边界。",
            detail={
                "filename": filename,
                "file_type": file_type,
                "mime_type": mime_type,
                "parser_name": self.parser_name,
                "parser_version": self.parser_version,
                "endpoint_configured": bool(self.endpoint),
                "timeout_seconds": self.timeout_seconds,
            },
            status_code=503,
        )
