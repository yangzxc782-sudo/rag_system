"""Selected GB/ISO document rules, independent of Text_cleaner's runtime."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from app.ingestion.mineru.normalizer import NormalizedDocumentBlock

CleaningProfile = Literal["auto", "gb_zh", "iso_en"]

GB_ID = r"GB(?:\s*/\s*T)?\s*\d+(?:\.\d+)*(?:\s*[—–:\-]\s*\d{4})?"
ISO_ID = r"ISO(?:\s*/\s*(?:ASTM|IEC))?\s*\d+(?:[.\-]\d+)*(?:\s*[:—–]\s*\d{4})?(?:\s*\(en\))?"
STANDARD_IDS = {"gb_zh": GB_ID, "iso_en": ISO_ID}

# These are whole heading names, never substring matches in ordinary body text.
REMOVED_SECTIONS = frozenset({
    "目录", "目次", "contents", "tableofcontents", "参考文献", "bibliography",
    "references", "前言", "foreword", "引言", "introduction",
})
CORE_SECTIONS = frozenset({
    "范围", "scope", "规范性引用文件", "normativereferences",
    "术语和定义", "术语与定义", "termsanddefinitions",
})
APPENDIX = re.compile(r"^(?:附\s*录\s*[A-Z]|Annex\s+[A-Z]|Appendix\s+[A-Z])\b", re.I)
NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)(?:[.)、])?\s+\S")

# Apply only to complete blocks in the confirmed cover before the first clause.
COVER_PATTERNS = {
    "gb_zh": tuple(re.compile(p, re.I) for p in (
        r"ICS\s+[\d.\s;]+", r"CCS\s+[A-Z]\s*\d+",
        r"中华人民共和国国家标准", r"国家市场监督管理总局(?:\s*发布)?",
        r"国家标准化管理委员会(?:\s*发布)?", r"(?:发布|实施)",
        r"\d{4}[-—/]\d{1,2}[-—/]\d{1,2}\s*(?:发布|实施)",
        r"[（(](?:报批稿|送审稿|征求意见稿|草案)[）)]",
    )),
    "iso_en": tuple(re.compile(p, re.I) for p in (
        r"COPYRIGHT PROTECTED DOCUMENT", r"©\s*ISO\b[^\n]*",
        r"Reference number(?:\s+" + ISO_ID + r")?",
    )),
}


def heading_name(title: str) -> str:
    title = re.sub(r"^#{1,6}\s+", "", title.strip())
    title = re.sub(r"^\d+(?:\.\d+)*(?:[.)、])?\s+", "", title)
    return re.sub(r"\s+", "", title).casefold()


def select_profile(
    blocks: Sequence[NormalizedDocumentBlock], requested: CleaningProfile, filename: str,
) -> str | None:
    if requested != "auto":
        if requested not in STANDARD_IDS:
            raise ValueError("Unsupported PDF cleaning profile")
        return requested
    first_page = min((b.page_start for b in blocks if b.page_start is not None), default=None)
    evidence: set[str] = set()
    for block in blocks[:32]:
        if block.page_start != first_page or block.block_type not in {"title", "text"}:
            continue
        value = (block.text or block.markdown or "").strip().lstrip("#").strip()
        if block.block_type == "title" and (
            NUMBERED_HEADING.match(value) or heading_name(value) in CORE_SECTIONS | REMOVED_SECTIONS
        ):
            break
        if any(NUMBERED_HEADING.match(s) or heading_name(s) in CORE_SECTIONS for s in block.section_path):
            continue
        if value == "中华人民共和国国家标准":
            evidence.add("gb_zh")
        for profile, pattern in STANDARD_IDS.items():
            if re.fullmatch(pattern, value, re.I):
                evidence.add(profile)
    if evidence:
        return next(iter(evidence)) if len(evidence) == 1 else None
    # A filename must begin with an actual standard number, not contain "ISO"/"GB".
    stem = Path(filename).stem.replace("_", " ")
    for profile, pattern in STANDARD_IDS.items():
        if re.match(r"^(?:" + pattern + r")(?=$|\s)", stem, re.I):
            evidence.add(profile)
    return next(iter(evidence)) if len(evidence) == 1 else None


def is_running_candidate(text: str, profile: str) -> bool:
    value = text.strip()
    return bool(re.fullmatch(STANDARD_IDS[profile], value, re.I)) or (
        profile == "iso_en" and value == "COPYRIGHT PROTECTED DOCUMENT"
    )
