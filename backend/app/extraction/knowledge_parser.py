from __future__ import annotations

import json
from json import JSONDecodeError
from uuid import UUID

from pydantic import ValidationError

from app.core.errors import KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED, BusinessError
from app.schemas.knowledge_item import ExtractedKnowledgeItem


def strip_json_code_fence(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_extraction_json(
    text: str,
    *,
    allowed_chunk_ids: set[UUID],
    allowed_item_types: set[str] | None = None,
) -> list[ExtractedKnowledgeItem]:
    try:
        payload = json.loads(strip_json_code_fence(text))
    except JSONDecodeError as exc:
        raise _parse_error("LLM extraction output is not valid JSON.", exc) from exc

    if not isinstance(payload, dict):
        raise _parse_error("LLM extraction output must be a JSON object.")
    if "items" not in payload:
        raise _parse_error("LLM extraction output must include items.")
    if not isinstance(payload["items"], list):
        raise _parse_error("LLM extraction output items must be a list.")

    parsed_items: list[ExtractedKnowledgeItem] = []
    for raw_item in payload["items"]:
        if not isinstance(raw_item, dict):
            raise _parse_error("Each extracted item must be a JSON object.")
        forbidden_fields = {"status", "approved"} & set(raw_item)
        if forbidden_fields:
            raise _parse_error(
                "Extracted items must not include status or approved fields.",
                detail={"forbidden_fields": sorted(forbidden_fields)},
            )
        try:
            item = ExtractedKnowledgeItem.model_validate(raw_item)
        except ValidationError as exc:
            raise _parse_error("Extracted item validation failed.", exc) from exc

        if allowed_item_types is not None and item.item_type not in allowed_item_types:
            raise _parse_error(
                "Extracted item_type is not allowed for this request.",
                detail={"item_type": item.item_type},
            )
        unknown_chunk_ids = [chunk_id for chunk_id in item.source_chunk_ids if chunk_id not in allowed_chunk_ids]
        if unknown_chunk_ids:
            raise _parse_error(
                "Extracted source_chunk_ids must come from input chunks.",
                detail={"source_chunk_ids": [str(chunk_id) for chunk_id in unknown_chunk_ids]},
            )
        parsed_items.append(item)
    return parsed_items


def _parse_error(message: str, exc: Exception | None = None, *, detail: object | None = None) -> BusinessError:
    error_detail = detail
    if error_detail is None and exc is not None:
        error_detail = {"error_type": exc.__class__.__name__}
    return BusinessError(
        KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED,
        message,
        detail=error_detail,
        status_code=500,
    )
