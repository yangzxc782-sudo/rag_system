"""Public, read-only projection of graph context already used by the answer."""

from app.rag.graph_context_builder import GraphContext
from app.schemas.rag import (
    RagGraphData, RagGraphDocument, RagGraphEntity, RagGraphEvidence,
    RagGraphRelationship, RagGraphTable,
)


def build_graph_response(context: GraphContext | None, *, triggered: bool) -> RagGraphData | None:
    if context is None or not context.enabled:
        return None

    evidence = []
    for item in context.evidence if triggered and context.status == "ok" else ():
        names = {entity.id: entity.name for entity in item.entities}
        evidence.append(RagGraphEvidence(
            graph_id=item.ref.graph_id,
            anchor_id=item.ref.anchor_id,
            anchor_type=item.ref.anchor_type,
            table_ref=item.ref.table_ref,
            source_citations=list(item.source_citations),
            # source_pdf may be a private path; expose only the graph document identity.
            document=RagGraphDocument(doc_id=item.document.doc_id),
            table=RagGraphTable(table_id=item.table.table_id, table_ref=item.table.table_ref,
                                page=item.table.page, table_index=item.table.table_index),
            entities=[RagGraphEntity(id=entity.id, name=entity.name, entity_type=entity.entity_type,
                                     page=entity.page) for entity in item.entities],
            relationships=[RagGraphRelationship(
                source_entity_id=edge.source_entity_id, source_name=names[edge.source_entity_id],
                type=edge.type, target_entity_id=edge.target_entity_id, target_name=names[edge.target_entity_id],
            ) for edge in item.relationships],
        ))

    if not triggered:
        status = "not_triggered"
    elif not evidence:
        status = "unavailable"
    elif any(item.status != "success" for item in context.diagnostics):
        status = "partial"
    else:
        status = "success"
    return RagGraphData(
        enabled=True, triggered=triggered, status=status, truncated=context.was_truncated,
        evidence_count=len(evidence), evidence=evidence,
    )
