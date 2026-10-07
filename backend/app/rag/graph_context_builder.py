from dataclasses import asdict, dataclass, replace
import json
from typing import Literal

from app.graph.models import GraphAnchorStatus, GraphEntity, GraphRelationship, GraphRetrievalResult, VerifiedAnchor


@dataclass(frozen=True)
class GraphEvidence:
    binding: VerifiedAnchor
    template_name: Literal["anchor_context_v2"]
    entities: tuple[GraphEntity, ...]
    relationships: tuple[GraphRelationship, ...]
    was_truncated: bool = False

    @property
    def ref(self):
        return self.binding.ref

    @property
    def source(self):
        return self.binding.source

    @property
    def provenance(self):
        return self.binding.provenance

    @property
    def source_citations(self) -> tuple[int, ...]:
        return tuple(sorted({p.citation_id for p in self.provenance}))


@dataclass(frozen=True)
class GraphDiagnostic:
    anchor_id: str
    graph_id: str
    mapped: bool
    query_status: GraphAnchorStatus
    full_unit_covered: bool
    facts_used: bool
    use_status: Literal["used", "incomplete_coverage", "query_failed", "graph_budget", "source_invalid", "prompt_omitted"]


@dataclass(frozen=True)
class GraphContext:
    enabled: bool = True
    status: Literal["ok", "empty", "disabled"] = "empty"
    evidence: tuple[GraphEvidence, ...] = ()
    total_chars: int = 0
    max_chars: int = 6000
    was_truncated: bool = False
    diagnostics: tuple[GraphDiagnostic, ...] = ()
    schema_version: Literal[2] = 2
    source_error: str | None = None


def build_graph_context(result: GraphRetrievalResult, *, max_chars=6000) -> GraphContext:
    if not result.enabled:
        return GraphContext(enabled=False, status="disabled", max_chars=max_chars)
    kept, diagnostics, used, truncated = [], [], 0, False
    for item in sorted(result.items, key=lambda i: (i.binding.ref.graph_id, i.binding.ref.anchor_id)):
        a = item.binding
        covered = a.fully_covered
        reason = "query_failed" if item.status != "success" else "incomplete_coverage" if not covered else "used"
        if reason == "used":
            evidence = GraphEvidence(a, item.template_name, item.entities, item.relationships)
            size = len(_format_evidence(evidence)) + (2 if kept else 0)
            if used + size > max(0, max_chars):
                reason, truncated = "graph_budget", True
            else:
                kept.append(evidence)
                used += size
        diagnostics.append(GraphDiagnostic(a.ref.anchor_id, a.ref.graph_id, True, item.status,
            covered, reason == "used", reason))
    return GraphContext(status="ok" if kept else "empty", evidence=tuple(kept), total_chars=used,
        max_chars=max_chars, was_truncated=truncated, diagnostics=tuple(diagnostics))


def retain_evidence(context, evidence, *, reason):
    keys = {(e.ref.graph_id, e.ref.anchor_id) for e in evidence}
    diagnostics = tuple(replace(d, facts_used=False, use_status=reason)
        if d.facts_used and (d.graph_id, d.anchor_id) not in keys else d for d in context.diagnostics)
    result = replace(context, evidence=tuple(evidence), diagnostics=diagnostics,
        status="ok" if evidence else "empty" if context.enabled else "disabled",
        was_truncated=context.was_truncated or len(evidence) != len(context.evidence))
    return replace(result, total_chars=len(format_graph_context_for_prompt(result)))


def format_graph_context_for_prompt(context):
    if not context.enabled or context.status != "ok":
        return ""
    return "\n\n".join(_format_evidence(item) for item in context.evidence)


def _canonical_properties(value):
    """Copy JSON properties with stable object keys across JSONB round trips.

    Lists and scalars keep their order, value and type; outer evidence layout
    is deliberately outside this normalization boundary.
    """
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Graph property keys must be strings")
        return {key: _canonical_properties(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_properties(item) for item in value]
    return value


def _format_evidence(item):
    s = item.source
    payload = dict(schema_version=2, template=item.template_name, anchor=item.ref.business_metadata(),
        source=dict(document_id=s.document_id, source_version=s.source_version, graph_build_id=s.graph_build_id,
                    unit_id=s.unit_id, source_start=s.source_start, source_end=s.source_end),
        source_citations=list(item.source_citations),
        entities=[{**asdict(e), "properties": _canonical_properties(e.properties)} for e in item.entities],
        relationships=[{**asdict(r), "properties": _canonical_properties(r.properties)} for r in item.relationships])
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
