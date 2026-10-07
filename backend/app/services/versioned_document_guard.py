"""Legacy operations cannot mutate documents that entered the frozen pipeline."""
from app.core.errors import BusinessError

FROZEN_PROCESS_STATUSES = frozenset({"cleaned_source_ready", "kg_extracting", "kg_writing", "kg_failed", "kg_ready",
    "kg_ready_empty", "chunking", "chunks_ready", "embedding", "indexing", "retrieval_indexed", "retrieval_failed"})


def require_legacy_document(document):
    if document.process_status in FROZEN_PROCESS_STATUSES:
        raise BusinessError("DOCUMENT_VERSIONED_PIPELINE_REQUIRED", "该文档需通过切片版本任务处理，旧入口不可修改。", status_code=409)
