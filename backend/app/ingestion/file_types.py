"""Product input types supported by the existing MinerU V4 upload contract.

Markdown is reserved for native ingestion and must never be sent to MinerU.
The deployment upload allowlist may narrow this set, but cannot expand it.
"""

MINERU_FILE_EXTENSIONS = frozenset(
    {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg", ".bmp", ".webp"}
)
MARKDOWN_FILE_EXTENSION = ".md"
DOCUMENT_FILE_EXTENSIONS = MINERU_FILE_EXTENSIONS | {MARKDOWN_FILE_EXTENSION}
