from __future__ import annotations

from typing import Any

from app.schemas.knowledge_item import KNOWLEDGE_ITEM_TYPES


def build_knowledge_extraction_system_prompt(item_types: list[str] | None = None) -> str:
    allowed_types = item_types or sorted(KNOWLEDGE_ITEM_TYPES)
    return "\n".join(
        [   "/no_think",
            "You are a knowledge extraction assistant for a casting process knowledge base.",
            "Extract only structured knowledge items that are explicitly supported by the provided chunks.",
            "Do not invent standards, clauses, process parameters, materials, causes, "
            "or solutions absent from the chunks.",
            "Return exactly one JSON object and no Markdown or explanatory text.",
            'The JSON object must use this top-level shape: {"items": [...]}.',
            "Each item must include item_type, title, content, confidence, and source_chunk_ids.",
            "Optional item fields are structured_data, entities, parameters, and conditions.",
            "source_chunk_ids must contain only chunk ids from the input chunks.",
            "If there is no extractable knowledge, return exactly: {\"items\": []}.",
            "Allowed item_type values: " + ", ".join(allowed_types) + ".",
            "confidence is extraction confidence only; it is not expert approval or trusted knowledge status.",
            "Do not output status, approved, review fields, prompt text, API keys, or system configuration.",
        ]
    )


def build_knowledge_extraction_user_prompt(chunks: list[Any], item_types: list[str] | None = None) -> str:
    allowed_types = item_types or sorted(KNOWLEDGE_ITEM_TYPES)
    parts = [
        "Allowed item_type values for this extraction:",
        ", ".join(allowed_types),
        "",
        "Source chunks:",
    ]
    for chunk in chunks:
        parts.extend(
            [
                f"[chunk_id: {getattr(chunk, 'id')}]",
                f"chunk_index: {getattr(chunk, 'chunk_index')}",
                "content:",
                str(getattr(chunk, "content", "") or ""),
                "",
            ]
        )
    parts.extend(
        [
            "Extract concise knowledge items from the chunks above.",
            "Return only JSON with top-level key items.",
        ]
    )
    return "\n".join(parts)


def build_knowledge_extraction_prompt(
    chunks: list[Any],
    item_types: list[str] | None = None,
) -> tuple[str, str]:
    return (
        build_knowledge_extraction_system_prompt(item_types),
        build_knowledge_extraction_user_prompt(chunks, item_types),
    )
