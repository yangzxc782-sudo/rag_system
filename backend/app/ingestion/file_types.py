"""Product admission, independent of a parser vendor's broader capabilities."""

DOCUMENT_FILE_EXTENSIONS = frozenset({".pdf"})


def is_pdf_content(content: bytes) -> bool:
    # Admission sniff only; full PDF validity remains the parser's responsibility.
    # Do not accept a renamed Office/image/Markdown file based on MIME metadata.
    return content.startswith(b"%PDF-")
