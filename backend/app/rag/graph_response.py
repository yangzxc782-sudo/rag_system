"""Project only evidence used by this prompt; diagnostics never masquerade as facts."""
from dataclasses import asdict
from app.schemas.rag import RagGraphData, RagGraphEvidence, RagGraphSource, RagGraphRelationship


def build_graph_response(context, *, triggered):
    if context is None or not context.enabled:
        return None
    evidence = []
    for item in context.evidence if triggered and context.status == "ok" else ():
        names = {e.id: e.name for e in item.entities}
        evidence.append(RagGraphEvidence(anchor=item.ref, source_citations=list(item.source_citations),
            source=RagGraphSource(**{k: getattr(item.source, k) for k in RagGraphSource.model_fields}),
            entities=[asdict(e) for e in item.entities], relationships=[RagGraphRelationship(**asdict(r),
                source_name=names[r.source_entity_id], target_name=names[r.target_entity_id]) for r in item.relationships]))
    used = {(e.anchor.graph_id, e.anchor.anchor_id) for e in evidence}
    diagnostics = [dict(asdict(d), facts_used=(d.graph_id, d.anchor_id) in used,
        use_status=d.use_status if not d.facts_used or (d.graph_id, d.anchor_id) in used else "prompt_omitted")
        for d in context.diagnostics]
    if context.source_error:
        status = "unavailable"
    elif not triggered:
        status = "not_triggered"
    elif not evidence:
        status = "not_used" if any(d.query_status == "success" for d in context.diagnostics) else "unavailable"
    else:
        status = "partial" if any(not d["facts_used"] for d in diagnostics) else "success"
    return RagGraphData(enabled=True, triggered=triggered, status=status, truncated=context.was_truncated,
        evidence_count=len(evidence), evidence=evidence, diagnostics=diagnostics, source_error=context.source_error)
