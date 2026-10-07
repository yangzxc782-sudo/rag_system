"""Sequential KG segmentation. Offsets are captured before any extraction view.

Adapted from the reference segment_md.py heading/anchor rules. This is not a
retrieval chunker; table pieces are model calls inside one immutable unit.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import PurePath
import re

from app.extraction.kg_protocol import ANCHOR_ADAPTER, ClauseAnchor, TableAnchor, format_anchor_end, format_anchor_start
from app.ingestion.frozen_source import json_bytes, sha256_bytes

RULE_VERSION = "kg-contiguous-v1"
RULE_SPEC = {"version": RULE_VERSION, "table_rows_per_piece": 40,
             "html": "raw-rows/rowspan-safe-pieces-or-intact", "clause": "heading/table-delimited",
             "coordinates": "unicode_code_point", "anchor_rules": "segment_md-v2",
             "plain_titles": "frozen-block-directory/numeric-depth-or-one"}
RULE_SHA256 = sha256_bytes(json_bytes(RULE_SPEC))
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NUM = re.compile(r"^(\d+(?:\.\d+)*)(?:\s+|$)(.*)")
_HTML = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.I | re.S)
_PIPE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


@dataclass(frozen=True)
class UnitDraft:
    source_start: int
    source_end: int
    anchor: TableAnchor | ClauseAnchor
    pieces: tuple[str, ...]

    @property
    def input_sha256(self) -> str:
        return sha256_bytes(json_bytes({"anchor": self.anchor.business_metadata(), "pieces": self.pieces}))


def make_graph_id(filename: str, identity_day: date) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "", PurePath(filename).stem)[:28] or "DOC"
    return f"KG-{identity_day:%Y%m%d}-{slug}"


def _slug(value: str) -> str:
    return re.sub(r"[^\w.\-]+", "", re.sub(r"\s+", "", value))[:24] or "节"


def _heading_context(stack: list[tuple[int, str]]) -> tuple[str, str]:
    if not stack:
        return "正文", ""
    number = _NUM.match(stack[-1][1])
    path = number[1] if number else "/".join(_slug(title) for _, title in stack[-3:])
    return path, " / ".join(title for _, title in stack)


def _table_pieces(body: str) -> tuple[str, ...]:
    # Preserve raw HTML/formulas. Split only simple row containers, never across
    # a rowspan. Complex wrappers stay intact rather than guessing cell meaning.
    if "<table" in body.lower():
        rows = list(re.finditer(r"<tr\b[^>]*>.*?</tr\s*>", body, re.I | re.S))
        if len(rows) <= 41:
            return (body,)
        if any(body[a.end():b.start()].strip() for a, b in zip(rows, rows[1:])):
            return (body,)
        span_ends = []
        for i, row in enumerate(rows):
            spans = re.findall(r'\browspan\s*=\s*[\"\x27]?(\d+)', row[0], re.I)
            if any(int(span) <= 0 or i + int(span) > len(rows) for span in spans):
                return (body,)
            span_ends.append(max([i + 1] + [i + int(span) for span in spans]))
        header_count = 0
        for row in rows:
            if not re.search(r"<th\b", row[0], re.I):
                break
            header_count += 1
        header_count = max(1, header_count)
        while max(span_ends[:header_count]) > header_count:
            header_count = max(span_ends[:header_count])
        if header_count == len(rows):
            return (body,)
        prefix = body[:rows[0].start()]
        header = body[rows[0].start():rows[header_count - 1].end()]
        suffix = body[rows[-1].end():]
        pieces, start = [], header_count
        while start < len(rows):
            end = min(start + 40, len(rows))
            while max(span_ends[start:end]) > end:
                end = max(span_ends[start:end])
            pieces.append(prefix + header + body[rows[start].start():rows[end - 1].end()] + suffix)
            start = end
        return tuple(pieces)
    lines = body.splitlines()
    header = next((i for i, line in enumerate(lines) if _PIPE_SEPARATOR.match(line)), None)
    if header is None:
        return (body,)
    data = lines[header + 1:]
    return tuple("\n".join(lines[:header + 1] + data[i:i + 40]) for i in range(0, len(data), 40)) or (body,)


def split_units(text: str, graph_id: str, *, block_directory: list[dict] | None = None) -> tuple[UnitDraft, ...]:
    """Scan source positions once; never find reconstructed strings in the source."""
    if re.search(r"<!--\s*kg-anchor-(?:start|end)\b", text, re.I):
        raise ValueError("KG controls are not canonical source")
    lines = text.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    offsets.append(offset)
    # Explicit table matches are original positions, including multiple tables
    # on one line. Reject malformed tables instead of silently losing a unit.
    tables = [(m.start(), m.end()) for m in _HTML.finditer(text)]
    if len(tables) != len(re.findall(r"<table\b", text, re.I)):
        raise ValueError("Unbalanced or nested HTML table")
    for i in range(len(lines) - 1):
        if _PIPE_SEPARATOR.match(lines[i + 1]) and "|" in lines[i]:
            end = i + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            start, stop = offsets[i], offsets[end]
            if not any(start < b and a < stop for a, b in tables):
                tables.append((start, stop))
    tables.sort()
    # Adjacent captions belong to the table's original continuous interval.
    for index, (start, end) in enumerate(tables):
        prior_lines = [(offsets[i], lines[i].strip()) for i in range(len(lines)) if offsets[i] < start]
        for pos, line in reversed(prior_lines):
            if not line:
                continue
            if line.startswith("表") and len(line) < 80 and text[pos:start].strip() == line:
                start = pos
            break
        tables[index] = (start, end)
    events = [(a, b, "table", None) for a, b in tables]
    title_ranges = []
    for block in block_directory or []:
        if block["block_type"] != "title":
            continue
        start, end = block["source_start"], block["source_end"]
        if not 0 <= start < end <= len(text) or any(start < b and a < end for a, b in tables):
            raise ValueError("Conflicting frozen title boundary")
        title = text[start:end].strip()
        md_heading = _HEADING.match(title)
        number = _NUM.match(title)
        level = len(md_heading[1]) if md_heading else min(number[1].count(".") + 1, 6) if number else 1
        events.append((start, end, "heading", (level, md_heading[2] if md_heading else title)))
        title_ranges.append((start, end))
    fence = None
    for i, line in enumerate(lines):
        if any(a <= offsets[i] < b for a, b in tables + title_ranges):
            continue
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
        match = _HEADING.match(stripped) if fence is None else None
        if match:
            events.append((offsets[i], offsets[i + 1], "heading", (len(match[1]), match[2])))
    events.sort()
    stack, counters, units = [], Counter(), []

    def emit(start: int, end: int, kind: str):
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start == end:
            return
        path, heading = _heading_context(stack)
        pid = re.sub(r"[^\w.\-]+", "-", re.sub(r"\s+", "", path).replace("/", "-")).strip("-") or "X"
        key = f"{'T' if kind == 'table' else 'C'}-{pid}"
        counters[key] += 1
        local = f"{key}-{counters[key]}"
        data = dict(anchor_id=f"{graph_id}::{local}", graph_id=graph_id, anchor_type=kind,
                    heading_ref=path, **{f"{kind}_ref": local})
        if heading:
            data["heading"] = heading
        body = text[start:end]
        if kind == "table":
            number = re.search(r"表\s*([0-9]+(?:\.[0-9]+)*)", body)
            if number:
                data["table_no"] = number[1]
        pieces = _table_pieces(body) if kind == "table" else (body,)
        units.append(UnitDraft(start, end, ANCHOR_ADAPTER.validate_python(data), pieces))

    cursor = 0
    for start, end, kind, heading in events:
        if start < cursor:
            raise ValueError("Overlapping KG structural boundaries")
        emit(cursor, start, "clause")
        if kind == "table":
            emit(start, end, kind)
        else:
            while stack and stack[-1][0] >= heading[0]:
                stack.pop()
            stack.append(heading)
        cursor = end
    emit(cursor, len(text), "clause")
    return tuple(units)


def render_tagged_source(text: str, units: tuple[UnitDraft, ...], eligible: set[str]) -> str:
    """Audit export inserts comments only; removing comments returns exact bytes."""
    output, cursor = [], 0
    for unit in units:
        if not 0 <= cursor <= unit.source_start < unit.source_end <= len(text):
            raise ValueError("Invalid unit range")
        output.append(text[cursor:unit.source_start])
        body = text[unit.source_start:unit.source_end]
        if unit.anchor.anchor_id in eligible:
            body = format_anchor_start(unit.anchor) + body + format_anchor_end(unit.anchor)
        output.append(body)
        cursor = unit.source_end
    output.append(text[cursor:])
    return "".join(output)
