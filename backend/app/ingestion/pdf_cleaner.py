"""PDF-only content transforms. No task, logging, scoring or export subsystem."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from app.ingestion.mineru.normalizer import NormalizedDocumentBlock
from app.ingestion.pdf_cleaner_rules import (
    APPENDIX, CORE_SECTIONS, COVER_PATTERNS, NUMBERED_HEADING, REMOVED_SECTIONS,
    CleaningProfile, heading_name, is_running_candidate, select_profile,
)


@dataclass(frozen=True, slots=True)
class PdfCleaningOptions:
    profile: CleaningProfile = "auto"
    backfill_enabled: bool = True
    filename: str = ""
    # Only the stable V4 page_idx contract enables PDF-page lookup. Do not guess.
    zero_based_pages: bool = False


_PROTECTED = re.compile(
    r"<table\b[^>]*>.*?</table\s*>|<table\b[^>]*>.*\Z"
    r"|^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^ {0,3}(?P=fence)[ \t]*(?=\n|$)"
    r"|`[^`\n]+`"
    r"|(?<!\\)\$\$.*?(?<!\\)\$\$"
    r"|(?<![\\$])\$(?!\$)[^\n]*?(?<!\\)\$(?!\$)"
    r"|\\\[.*?\\\]|\\\(.*?\\\)"
    r"|!\[[^\]\n]*\]\([^\n]*?\)"
    r"|^[ \t]*\|[^\n]*(?:\n[ \t]*\|[^\n]*)*",
    re.S | re.M | re.I,
)
_LINE_BREAK = re.compile(
    r"^(?:#{1,6}\s|[-*+•]\s|\d+[.)、]\s|[a-zA-Z][)）]\s|>\s|"
    r"\d+(?:\.\d+)+\s|(?:图|表|Figure|Table|Fig\.)\s*[\dA-Z])", re.I,
)
_SENSITIVE = re.compile(r"[\d$\\<>=≤≥±%‰]|\b(?:not|no|shall|must)\b|[不无非未勿禁止]", re.I)


def clean_pdf_blocks(
    blocks: Sequence[NormalizedDocumentBlock], pdf_bytes: bytes, options: PdfCleaningOptions,
) -> list[NormalizedDocumentBlock]:
    """Return independent, source-preserving blocks; never reparse with MinerU."""
    result = deepcopy(list(blocks))
    profile = select_profile(result, options.profile, options.filename)
    for block in result:
        _repair_heading(block)
    has_headings = any(block.block_type == "title" for block in result)
    result = _filter_sections(result, profile)
    pages: list[tuple[str, str]] = []
    if options.zero_based_pages and (options.backfill_enabled or profile is not None):
        pages = _read_pdf_pages(pdf_bytes)
    # Include filtered blocks: backfill must not reintroduce removed material or
    # borrow an adjacent block's content just because PDF reading order interleaves it.
    other_page_texts: dict[int, list[tuple[int, str]]] = {}
    if pages and options.backfill_enabled:
        for original in blocks:
            if original.page_start is not None and original.page_start == original.page_end:
                for value in (original.text, original.markdown, original.caption, original.latex):
                    compact = re.sub(r"\s+", "", value or "")
                    if compact:
                        other_page_texts.setdefault(original.page_start, []).append((original.block_index, compact))
    running_pages: dict[str, set[int]] = {}
    if profile is not None:
        for block in result:
            value = (block.text or block.markdown or "").strip()
            page = block.page_start
            if (
                block.block_type == "text" and page is not None and 0 <= page < len(pages)
                and block.page_end == page and is_running_candidate(value, profile)
                and not any(heading_name(s) == "normativereferences" or heading_name(s) == "规范性引用文件" for s in block.section_path)
                and pages[page][0].splitlines().count(value) == 1
                and value in pages[page][1].splitlines()
            ):
                running_pages.setdefault(value, set()).add(page)
    kept: list[NormalizedDocumentBlock] = []
    for block in result:
        value = block.markdown or block.text or ""
        running_key = value.strip()
        if block.block_type == "text" and len(running_pages.get(running_key, ())) >= 2:
            # Normative references in the body are not headers, even if repeated.
            page = block.page_start
            if page is not None and page in running_pages[running_key]:
                continue
        if block.block_type in {"text", "list", "unknown"} and not _is_code(block):
            if options.backfill_enabled and _can_backfill(block, value, pages):
                excluded = [text for index, text in other_page_texts.get(block.page_start, ()) if index != block.block_index]
                value = _backfill_text(value, pages[block.page_start][0], excluded)
            value = _normalize_prose(value, merge_lines=block.block_type == "text")
            # Both views refer to cleaned content. The shared renderer selects only one.
            block.text = value
            if block.markdown is not None:
                block.markdown = value
        kept.append(block)
    if has_headings:
        _rebuild_sections(kept)
    return kept


def _is_code(block: NormalizedDocumentBlock) -> bool:
    return block.source_metadata.get("original_type") in {"code", "algorithm"}


def _repair_heading(block: NormalizedDocumentBlock) -> None:
    if _is_code(block):
        return
    value = (block.text or block.markdown or "").strip()
    marked = re.fullmatch(r"(#{1,6})[ \t]+([^\n]+)", value)
    if block.block_type != "title" and not (block.block_type == "text" and marked):
        return
    block.block_type = "title"
    title = marked.group(2).strip() if marked else value
    numbered = NUMBERED_HEADING.match(title)
    level = (
        min(6, numbered.group(1).count(".") + 1) if numbered else
        1 if APPENDIX.match(title) else
        len(marked.group(1)) if marked else block.source_metadata.get("heading_level", 1)
    )
    level = max(1, min(6, int(level or 1)))
    block.text = title
    block.markdown = f"{'#' * level} {title}"
    block.source_metadata["heading_level"] = level


def _filter_sections(
    blocks: list[NormalizedDocumentBlock], profile: str | None,
) -> list[NormalizedDocumentBlock]:
    kept: list[NormalizedDocumentBlock] = []
    removed_level: int | None = None
    first_page = min((b.page_start for b in blocks if b.page_start is not None), default=None)
    in_cover = True
    for block in blocks:
        if block.block_type in {"header", "footer"}:
            continue
        title = block.text or ""
        if block.block_type == "title":
            level = block.source_metadata["heading_level"]
            core = heading_name(title) in CORE_SECTIONS
            appendix = bool(APPENDIX.match(title))
            if core or appendix or NUMBERED_HEADING.match(title):
                in_cover = False
            if removed_level is not None and (level <= removed_level or core or appendix):
                removed_level = None
            if profile is not None and removed_level is None and heading_name(title) in REMOVED_SECTIONS:
                removed_level = level
        if removed_level is not None:
            continue
        if (
            profile is not None and in_cover and block.page_start == first_page
            and block.block_type in {"text", "title"}
            and any(p.fullmatch((block.text or "").strip()) for p in COVER_PATTERNS[profile])
        ):
            continue
        kept.append(block)
    return kept


def _normalize_prose(text: str, *, merge_lines: bool) -> str:
    # An unterminated code fence or unmatched math delimiter is preserved, not guessed.
    if (
        len(re.findall(r"(?m)^\s*(?:`{3,}|~{3,})", text)) % 2
        or re.search(r"(?m)^(?: {4}|\t)", text)
        # Pipe tables may omit their outside pipes; keep such blocks verbatim.
        or re.search(r"(?m)^\s*\|?\s*:?-{3,}:?\s*\|\s*:?-{3,}", text)
    ):
        return text
    marker = "\x00PDF_SPAN_"
    while marker in text:
        marker += "_"
    spans: list[str] = []

    def protect(match: re.Match[str]) -> str:
        spans.append(match.group())
        return f"{marker}{len(spans) - 1}\x00"

    working = _PROTECTED.sub(protect, text)
    if re.search(r"(?<!\\)\$|\\[\[(]", working):
        return text
    lines: list[str] = []
    previous_can_merge = False
    for raw in working.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith(("    ", "\t")) or raw.endswith("  "):
            lines.append(raw)
            previous_can_merge = False
            continue
        line = re.sub(r"[ \t]+", " ", raw.strip())
        line = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", line)
        can_merge = bool(line) and not _LINE_BREAK.match(line) and marker not in line
        if merge_lines and previous_can_merge and can_merge and not re.search(r"[。！？.!?;；:：]$", lines[-1]):
            separator = "" if re.search(r"[\u4e00-\u9fff]$", lines[-1]) and re.match(r"[\u4e00-\u9fff]", line) else " "
            lines[-1] += separator + line
        elif line or not lines or lines[-1]:
            lines.append(line)
        previous_can_merge = can_merge
    result = "\n".join(lines).strip()
    for index, original in enumerate(spans):
        token = f"{marker}{index}\x00"
        if result.count(token) != 1:
            raise ValueError("PDF protected content was lost or duplicated")
        result = result.replace(token, original)
    if marker in result:
        raise ValueError("PDF protected content was not restored")
    return result


def _rebuild_sections(blocks: list[NormalizedDocumentBlock]) -> None:
    stack: list[tuple[int, str]] = []
    for block in blocks:
        if block.block_type == "title":
            level = block.source_metadata["heading_level"]
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, block.text or ""))
        block.section_path = [title for _, title in stack]


def _read_pdf_pages(content: bytes) -> list[tuple[str, str]]:
    """Read text and physical margins in PDF coordinates; scanned pages yield empty text."""
    import pdfplumber
    from pdfminer.pdfparser import PDFSyntaxError
    from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect

    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            pages = []
            for page in pdf.pages:
                x0, top, x1, bottom = page.bbox
                band = (bottom - top) * 0.1
                margins = "\n".join((
                    page.crop((x0, top, x1, top + band)).extract_text() or "",
                    page.crop((x0, bottom - band, x1, bottom)).extract_text() or "",
                ))
                pages.append((page.extract_text() or "", margins))
            return pages
    except (PDFSyntaxError, PDFEncryptionError, PDFPasswordIncorrect):
        # An unreadable independent text layer supplies no evidence for deletion/backfill.
        return []


def _can_backfill(block: NormalizedDocumentBlock, text: str, pages: list[tuple[str, str]]) -> bool:
    page = block.page_start
    return (
        block.block_type == "text" and page is not None and page == block.page_end
        and 0 <= page < len(pages) and bool(pages[page][0]) and len(text) >= 24
        and not _PROTECTED.search(text) and not re.search(r"[#|<>$\\]", text)
    )


def _backfill_text(text: str, page_text: str, excluded: Sequence[str] = ()) -> str:
    """Pure insertion between exact unique anchors; never replace conflicting values."""
    # Collapse whitespace only for alignment, retaining a map to the original PDF bytes' text.
    source_positions = [i for i, c in enumerate(page_text) if not c.isspace()]
    source = "".join(page_text[i] for i in source_positions)
    target_positions = [i for i, c in enumerate(text) if not c.isspace()]
    target = "".join(text[i] for i in target_positions)
    if len(target) < 24 or target in source:
        return text
    anchor_size = 12
    left, right = target[:anchor_size], target[-anchor_size:]
    if source.count(left) != 1 or source.count(right) != 1:
        return text
    start = source.index(left)
    end = source.index(right) + anchor_size
    if end <= start or end - start > max(len(target) * 2, len(target) + 120):
        return text
    candidate = source[start:end]
    changes = SequenceMatcher(None, target, candidate, autojunk=False).get_opcodes()
    insertions: list[tuple[int, str]] = []
    for tag, i, j, a, b in changes:
        if tag == "equal":
            continue
        if tag != "insert" or i < anchor_size or i > len(target) - anchor_size:
            return text
        before, after = target[i - anchor_size:i], target[i:i + anchor_size]
        if candidate.count(before) != 1 or candidate.count(after) != 1:
            return text
        # Never complete a partial number, unit, comparator or negation by guessing.
        if _SENSITIVE.search(target[i - 1:i + 1]):
            return text
        pdf_start = source_positions[start + a - 1] + 1
        pdf_end = source_positions[start + b]
        addition = page_text[pdf_start:pdf_end]
        compact_addition = re.sub(r"\s+", "", addition)
        if any(other in compact_addition or compact_addition in other for other in excluded):
            return text
        if re.search(r"[$\\<>|]|\b(?:not|no|never)\b|[不无非未勿]|禁止", addition, re.I):
            return text
        # Inserting characters inside a unit/word is correction, not missing-text backfill.
        left_char = page_text[source_positions[start + a - 1]]
        right_char = page_text[source_positions[start + b]]
        if (
            left_char.isascii() and left_char.isalnum() and addition[:1].isalnum()
            or right_char.isascii() and right_char.isalnum() and addition[-1:].isalnum()
        ):
            return text
        insertions.append((target_positions[i], addition))
    for offset, addition in reversed(insertions):
        text = text[:offset] + addition + text[offset:]
    return text


def _value(block: Any, name: str, default: Any = None) -> Any:
    return block.get(name, default) if isinstance(block, Mapping) else getattr(block, name, default)


def _formula_body(value: str) -> str:
    value = value.strip()
    for start, end in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$")):
        if start == "$" and (value.startswith("$$") or value.endswith("$$")):
            continue
        if value.startswith(start) and value.endswith(end) and len(value) >= len(start) + len(end):
            return value[len(start):-len(end)].strip()
    return value


def _join_unique(*values: str) -> str:
    return "\n\n".join(dict.fromkeys(v.strip() for v in values if v and v.strip()))


def render_cleaned_block(block: Any, *, output_prefix: str = "") -> tuple[str, str]:
    """Shared by Markdown export and chunking of both normalized and persisted blocks."""
    kind = _value(block, "block_type", "unknown")
    text = _value(block, "text") or ""
    markdown = _value(block, "markdown") or ""
    html = _value(block, "html") or ""
    latex = _value(block, "latex") or ""
    caption = _value(block, "caption") or ""
    metadata = _value(block, "source_metadata") or {}
    if kind in {"header", "footer"}:
        return "", "plain_text"
    if kind == "table":
        return _join_unique(caption, html or markdown or text), "markdown"
    if kind == "formula":
        if not latex:
            return markdown or text, "markdown" if markdown else "plain_text"
        body = _formula_body(latex)
        # A malformed existing wrapper is not permission to invent its missing delimiter.
        formula = latex if re.search(r"(?<!\\)\$|\\[\[\]()]", body) else f"$$\n{body}\n$$"
        description = text if _formula_body(text) != body else ""
        return _join_unique(description, formula), "markdown"
    if kind == "image":
        keys = _value(block, "asset_keys") or metadata.get("asset_keys", [])
        links = []
        for key in keys:
            relative = str(key)
            if output_prefix:
                prefix = output_prefix.rstrip("/") + "/"
                if not relative.startswith(prefix):
                    raise ValueError("PDF image reference escapes its parse run")
                relative = relative[len(prefix):]
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
                raise ValueError("Invalid PDF image reference")
            links.append(f"![]({quote(relative, safe='/')})")
        return _join_unique("\n".join(links), caption, text), "markdown"
    if kind == "title":
        return markdown or text, "markdown"
    if not markdown and (metadata.get("original_type") in {"code", "algorithm"} or re.match(r"^(?: {4}|\t)", text)):
        fence = "```"
        while fence in text:
            fence += "`"
        return f"{fence}\n{text}\n{fence}", "markdown"
    return markdown or text or caption, "markdown" if markdown else "plain_text"
