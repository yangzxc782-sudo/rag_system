"""Immutable embedding identity and finite, PostgreSQL float32 vector contract."""
import math
import struct

from app.ingestion.frozen_source import json_bytes, sha256_bytes


def embedding_fingerprint(settings) -> str:
    revision = str(getattr(settings, "pdf_kg_embedding_revision", "")).strip()
    if not revision or int(settings.embedding_dim) != 1024:
        raise ValueError("Versioned embeddings require an explicit model revision and dimension 1024")
    keys = ("embedding_provider", "embedding_model", "embedding_dim", "embedding_model_path",
            "embedding_normalize", "embedding_query_instruction", "embedding_use_query_instruction")
    return sha256_bytes(json_bytes({**{k: getattr(settings, k) for k in keys},
                                   "model_revision": revision, "contract": "pg-float32-v1"}))


def vector32(values, dimension: int) -> list[float]:
    if len(values) != dimension:
        raise ValueError("Embedding dimension mismatch")
    output = []
    for item in values:
        if isinstance(item, bool) or not math.isfinite(float(item)):
            raise ValueError("Nonfinite embedding")
        value = struct.unpack("f", struct.pack("f", float(item)))[0]
        if not math.isfinite(value):
            raise ValueError("Embedding overflows float32")
        output.append(value)
    return output
