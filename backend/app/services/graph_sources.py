"""Read-only authority for final text -> continuous KG units; no object-store/model IO."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.extraction.kg_extract import TEMPLATE_SHA256, template
from app.extraction.kg_protocol import ANCHOR_ADAPTER, validate_anchors
from app.graph.models import GraphSource, GraphTriggerProvenance, VerifiedAnchor
from app.ingestion.frozen_source import sha256_bytes
from app.ingestion.source_intervals import overlaps
from app.models import Document, DocumentChunk, GraphBuild, KGExtractionUnit, SourceDocumentVersion
from app.models.document_chunk_set import ChunkSet
from app.rag.context_builder import TRUNCATION_MARKER
from app.services.retrieval_admission import _valid_receipt, valid_structure


def valid_prefix(chunk, stored_content: str) -> bool:
    start, end = chunk.effective_start, chunk.effective_end
    if (type(start) is not int or type(end) is not int or start != chunk.source_start
            or not start <= end <= chunk.source_end):
        return False
    length = end - start
    if not chunk.was_truncated:
        return length == len(stored_content) and chunk.content == stored_content
    if length >= len(stored_content):
        return False
    if length == 0:
        return chunk.content == TRUNCATION_MARKER[:len(chunk.content)] and len(chunk.content) <= len(TRUNCATION_MARKER)
    return chunk.content == stored_content[:length] + TRUNCATION_MARKER


class GraphSourceAuthority:
    def __init__(self, settings, session_factory=None):
        self.settings, self.session_factory = settings, session_factory

    def resolve(self, context, *, current=True):
        if not context.chunks:
            return ()
        if len(context.chunks) > 50:
            raise ValueError("Graph source budget exceeded")
        factory = self.session_factory
        if factory is None:
            from app.db.session import SessionLocal
            from sqlalchemy.engine import make_url
            if SessionLocal.kw["bind"].url != make_url(self.settings.database_url):
                raise ValueError("Graph source database mismatch")
            factory = SessionLocal
        with factory() as db:
            return self.resolve_in_session(db, context, current=current)

    def resolve_in_session(self, db, context, *, current):
        # No row locks and no commit of caller-owned transactions (recovery uses this).
        bindings, builds = {}, {}
        seen_chunks, seen_citations = set(), set()
        for c in context.chunks:
            if c.chunk_set_id is None:
                raise ValueError("Legacy evidence is not graph-admissible")
            ids = [UUID(v) for v in (c.document_id, c.chunk_id, c.source_version, c.graph_build_id, c.chunk_set_id)]
            if c.chunk_id in seen_chunks or type(c.citation_id) is not int or c.citation_id < 1 or c.citation_id in seen_citations:
                raise ValueError("Duplicate/invalid final citation")
            seen_chunks.add(c.chunk_id)
            seen_citations.add(c.citation_id)
            query = select(Document, DocumentChunk, ChunkSet, GraphBuild, SourceDocumentVersion).select_from(DocumentChunk).join(
                Document, Document.id == DocumentChunk.document_id).join(ChunkSet, ChunkSet.id == DocumentChunk.chunk_set_id).join(
                GraphBuild, GraphBuild.id == ChunkSet.graph_build_id).join(
                SourceDocumentVersion, SourceDocumentVersion.source_version == ChunkSet.source_version).where(
                DocumentChunk.id == ids[1]).options(undefer("*"))
            row = db.execute(query).one_or_none()
            if row is None:
                raise ValueError("Graph source unavailable")
            doc, chunk, parent, build, source = row
            if (doc.id != ids[0] or doc.deletion_status != "normal" or doc.file_type != ".pdf"
                    or parent.id != ids[4] or build.id != ids[3] or source.source_version != ids[2]
                    or any(r.document_id != doc.id for r in (chunk, parent, build, source))
                    or any(r.source_version != source.source_version for r in (chunk, parent, build))
                    or chunk.parse_run_id != source.parse_run_id
                    or (current and doc.current_chunk_set_id != parent.id)
                    or parent.status != "indexed" or parent.sealed_at is None or parent.indexed_at is None
                    or not _valid_receipt(parent) or not valid_structure(chunk, parent) or chunk.embedding_status != "embedded"
                    or build.status not in ("ready", "ready_empty") or build.sealed_at is None
                    or build.graph_schema_version != 2 or build.template_sha256 != TEMPLATE_SHA256
                    or build.template_version != template()["version"]
                    or source.coordinate_unit != "unicode_code_point" or source.normalization != "LF_NFC_UTF8_v1"):
                raise ValueError("Graph source ownership/admission mismatch")
            if (c.source_start != chunk.source_start or c.source_end != chunk.source_end
                    or c.content_sha256 != chunk.content_sha256 or c.embedding_fingerprint != parent.embedding_fingerprint
                    or chunk.content_sha256 != sha256_bytes(chunk.content.encode("utf-8"))
                    or not 0 <= chunk.source_start < chunk.source_end <= source.character_count
                    or len(chunk.content) != chunk.source_end - chunk.source_start
                    or c.source_metadata != chunk.source_metadata or not valid_prefix(c, chunk.content)):
                raise ValueError("Graph source content/prefix mismatch")
            key = str(build.id)
            if key not in builds:
                units = list(db.scalars(select(KGExtractionUnit).where(
                    KGExtractionUnit.graph_build_id == build.id).order_by(KGExtractionUnit.unit_index).limit(self.settings.kg_max_units + 1)))
                if len(units) != build.unit_count or len(units) > self.settings.kg_max_units:
                    raise ValueError("Incomplete anchor index")
                entries, last_end = [], 0
                for unit in units:
                    ref = ANCHOR_ADAPTER.validate_python(unit.allocated_anchor_metadata)
                    if (unit.document_id != doc.id or unit.source_version != source.source_version
                            or unit.status not in ("succeeded_empty", "succeeded_nonempty")
                            or unit.has_qualified_triples != (unit.status == "succeeded_nonempty")
                            or unit.completed_at is None or not unit.result_sha256
                            or ref.graph_id != build.graph_id or ref.anchor_id != unit.allocated_anchor_id
                            or ref.anchor_type != unit.kind or not last_end <= unit.source_start < unit.source_end <= source.character_count):
                        raise ValueError("Inconsistent anchor index")
                    last_end = unit.source_end
                    if unit.has_qualified_triples:
                        entries.append((unit, ref))
                if len(entries) != build.anchor_count:
                    raise ValueError("Anchor count mismatch")
                if entries:
                    receipt = (build.write_checkpoint or {}).get("receipt")
                    if receipt != dict(payload_sha256=build.result_sha256, entities=build.entity_count,
                            relationships=build.relationship_count, graph_build_id=str(build.id), status="built"):
                        raise ValueError("Graph write not verified")
                builds[key] = entries
            entries = builds[key]
            # Indexed refs must exactly match the immutable full chunk. Effective refs
            # below come only from the authoritative units, not metadata assertions.
            expected = [ref for u, ref in entries if overlaps(chunk.source_start, chunk.source_end, u.source_start, u.source_end)]
            actual = validate_anchors((chunk.source_metadata or {}).get("kg_refs", []))
            if {r.anchor_id: r for r in actual} != {r.anchor_id: r for r in expected}:
                raise ValueError("Conflicting chunk anchor metadata")
            for unit, ref in entries:
                if not overlaps(c.effective_start, c.effective_end, unit.source_start, unit.source_end):
                    continue
                source_ref = GraphSource(str(doc.id), str(source.source_version), str(build.id), str(unit.id),
                    unit.source_start, unit.source_end, source.canonical_sha256, build.result_sha256,
                    build.source_path, build.template_version)
                origin = GraphTriggerProvenance(ref.anchor_id, c.document_id, c.chunk_id, c.citation_id,
                    c.source_version, c.graph_build_id, c.chunk_set_id, c.effective_start, c.effective_end)
                identity = (ref.graph_id, ref.anchor_id)
                previous = bindings.get(identity)
                if previous and (previous.ref != ref or previous.source != source_ref):
                    raise ValueError("Conflicting anchor identity")
                bindings[identity] = VerifiedAnchor(ref, source_ref, (*previous.provenance, origin) if previous else (origin,))
        return tuple(bindings.values())


def validate_evidence_bindings(graph, bindings):
    expected = {(a.ref.graph_id, a.ref.anchor_id): a for a in bindings}
    return all((item.binding == expected.get((item.ref.graph_id, item.ref.anchor_id))
                and item.binding.fully_covered) for item in graph.evidence)
