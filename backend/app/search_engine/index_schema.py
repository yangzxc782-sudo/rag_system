from __future__ import annotations

from typing import Any


def get_index_name(settings: Any) -> str:
    return str(settings.search_index_name).strip()


def get_index_alias(settings: Any) -> str:
    return str(settings.search_index_alias).strip()


def build_casting_chunks_index_settings(settings: Any) -> dict[str, Any]:
    return {
        "index": {
            "knn": True,
        }
    }


def build_casting_chunks_index_mapping(settings: Any) -> dict[str, Any]:
    content_analyzer = str(settings.search_content_analyzer).strip() or "ik_max_word"
    query_analyzer = str(settings.search_query_analyzer).strip() or "ik_smart"
    vector_space = _opensearch_space_type(settings.search_vector_space)

    text_with_raw = {
        "type": "text",
        "fields": {
            "raw": {
                "type": "keyword",
                "ignore_above": 256,
            }
        },
    }

    return {
        "dynamic": False,
        "properties": {
            "chunk_id": {"type": "keyword"},
            "document_id": {"type": "keyword"},
            "original_filename": text_with_raw,
            "chunk_index": {"type": "integer"},
            "content": {
                "type": "text",
                "analyzer": content_analyzer,
                "search_analyzer": query_analyzer,
                "fields": {
                    "max": {
                        "type": "text",
                        "analyzer": "ik_max_word",
                        "search_analyzer": query_analyzer,
                    },
                    "smart": {
                        "type": "text",
                        "analyzer": "ik_smart",
                        "search_analyzer": "ik_smart",
                    },
                },
            },
            "content_max": {
                "type": "text",
                "analyzer": "ik_max_word",
                "search_analyzer": query_analyzer,
            },
            "content_smart": {
                "type": "text",
                "analyzer": "ik_smart",
                "search_analyzer": "ik_smart",
            },
            "exact_terms": {"type": "keyword"},
            "chunk_type": {"type": "keyword"},
            "page_start": {"type": "integer"},
            "page_end": {"type": "integer"},
            "section_title": text_with_raw,
            "source_metadata": {
                "type": "object",
                "enabled": False,
            },
            "embedding": {
                "type": "knn_vector",
                "dimension": int(settings.embedding_dim),
                "space_type": vector_space,
            },
            "embedding_model": {"type": "keyword"},
            "embedding_dim": {"type": "integer"},
            "embedding_status": {"type": "keyword"},
            "document_process_status": {"type": "keyword"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        },
    }


def build_casting_chunks_index_body(settings: Any) -> dict[str, Any]:
    alias = get_index_alias(settings)
    return {
        "settings": build_casting_chunks_index_settings(settings),
        "mappings": build_casting_chunks_index_mapping(settings),
        "aliases": {
            alias: {},
        },
    }


def _opensearch_space_type(search_vector_space: str) -> str:
    normalized = str(search_vector_space).strip().lower()
    if normalized == "cosine":
        return "cosinesimil"
    return normalized
