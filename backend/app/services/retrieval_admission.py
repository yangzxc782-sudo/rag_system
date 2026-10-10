"""Explicit v2 retrieval admission; no legacy or stale-set fallback."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.core.errors import BusinessError
from app.ingestion.block_chunker import frozen_config
from app.models import Document, DocumentChunk, GraphBuild
from app.models.document_chunk_set import ChunkSet
from app.search_engine.versioned_index import index_name
from app.services.embedding_contract import embedding_fingerprint

VERSION_FIELDS = ("source_version", "graph_build_id", "chunk_set_id", "source_start", "source_end", "content_sha256", "embedding_fingerprint")
STRUCTURE_FIELDS = ("chunk_type", "section_title", "content_format", "chunk_method", "page_start", "page_end", "token_count", "parse_run_id")


def structure_fields(chunk):
    return {k: str(chunk.parse_run_id) if k == "parse_run_id" else getattr(chunk, k) for k in STRUCTURE_FIELDS}


def valid_structure(chunk, parent):
    metadata = chunk.source_metadata or {}
    return (isinstance(metadata, dict) and chunk.chunk_method == parent.segmentation_version
        and all(k in metadata and metadata[k] == v for k, v in structure_fields(chunk).items())
        and metadata.get("segmentation_version") == parent.segmentation_version
        and metadata.get("segmentation_config_sha256") == parent.segmentation_config_sha256)


def _published(fingerprint):
    return select(ChunkSet).join(Document, Document.current_chunk_set_id == ChunkSet.id).join(
        GraphBuild, GraphBuild.id == ChunkSet.graph_build_id).where(
        ChunkSet.document_id == Document.id, Document.deletion_status == "normal", Document.file_type == ".pdf",
        ChunkSet.status == "indexed", ChunkSet.sealed_at.is_not(None), ChunkSet.indexed_at.is_not(None),
        ChunkSet.embedding_fingerprint == fingerprint, GraphBuild.document_id == Document.id,
        GraphBuild.source_version == ChunkSet.source_version, GraphBuild.graph_schema_version == 2,
        GraphBuild.status.in_(("ready", "ready_empty")), GraphBuild.sealed_at.is_not(None))


def _valid_receipt(row):
    try:
        frozen_config(row.segmentation_config, row.segmentation_config_sha256, row.segmentation_version)
    except (TypeError, ValueError):
        return False
    receipt = row.index_receipt or {}
    return (row.index_name == index_name(row.id) and receipt.get("verified") is True and receipt.get("write_blocked") is True
        and all(receipt.get(k) == v for k, v in dict(chunk_set_id=str(row.id), document_id=str(row.document_id),
            source_version=str(row.source_version), graph_build_id=str(row.graph_build_id), schema_version=2,
            index_name=row.index_name, embedding_fingerprint=row.embedding_fingerprint,
            manifest_sha256=row.manifest_sha256, chunk_count=row.chunk_count).items()))


def published_targets(session_factory, settings, document_id=None):
    try:
        fingerprint = embedding_fingerprint(settings)
        with session_factory() as db:
            query = _published(fingerprint)
            if document_id is not None:
                query = query.where(Document.id == document_id)
            rows = list(db.scalars(query.order_by(ChunkSet.id).limit(settings.pdf_kg_max_search_indices + 1)))
            if len(rows) > settings.pdf_kg_max_search_indices or any(not _valid_receipt(row) for row in rows):
                raise ValueError("Invalid or over-budget published index set")
            return {str(row.id): row.index_name for row in rows}
    except Exception as exc:
        raise BusinessError("PDF_KG_ADMISSION_UNAVAILABLE", "新版检索准入未就绪。", status_code=503) from exc


def filter_published_hits(keyword, vector, session_factory, settings, targets):
    ids = set()
    for hit in (*keyword, *vector):
        try:
            ids.add(UUID(hit.chunk_id))
        except ValueError:
            pass
    if not ids:
        return [], []
    allowed = {}
    with session_factory() as db:
        query = _published(embedding_fingerprint(settings)).add_columns(DocumentChunk, Document.original_filename).join(
            DocumentChunk, DocumentChunk.chunk_set_id == ChunkSet.id).where(DocumentChunk.id.in_(ids)).options(undefer("*"))
        for parent, chunk, filename in db.execute(query):
            if not _valid_receipt(parent) or targets.get(str(parent.id)) != parent.index_name:
                continue
            if (chunk.document_id != parent.document_id or chunk.source_version != parent.source_version
                    or chunk.embedding_status != "embedded" or not valid_structure(chunk, parent)):
                continue
            allowed[str(chunk.id)] = (parent.index_name, dict(chunk_id=str(chunk.id), document_id=str(chunk.document_id),
                original_filename=filename,
                chunk_index=chunk.chunk_index, content=chunk.content, source_metadata=chunk.source_metadata,
                **structure_fields(chunk),
                source_version=str(chunk.source_version), graph_build_id=str(parent.graph_build_id), chunk_set_id=str(parent.id),
                source_start=chunk.source_start, source_end=chunk.source_end, content_sha256=chunk.content_sha256,
                embedding_fingerprint=parent.embedding_fingerprint, schema_version=2,
                embedding_model=chunk.embedding_model, embedding_dim=chunk.embedding_dim))
    def keep(hit):
        record = allowed.get(hit.chunk_id)
        return record is not None and hit.index_name == record[0] and all(hit.source.get(k) == v for k, v in record[1].items())
    return [h for h in keyword if keep(h)], [h for h in vector if keep(h)]
