from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import json
from typing import Literal

from app.graph.models import (
    GraphAnchorResult, GraphAnchorStatus, GraphDocument, GraphEntity,
    GraphRelationship, GraphRetrievalResult, GraphTable, GraphTriggerProvenance, KGRef,
)


@dataclass(frozen=True)
class GraphEvidence:
    ref: KGRef
    template_name: str
    document: GraphDocument
    table: GraphTable
    entities: tuple[GraphEntity, ...]
    relationships: tuple[GraphRelationship, ...]
    provenance: tuple[GraphTriggerProvenance, ...]
    was_truncated: bool = False

    @property
    def source_citations(self) -> tuple[int, ...]:
        return tuple(sorted({p.citation_id for p in self.provenance if p.citation_id is not None}))


@dataclass(frozen=True)
class GraphDiagnostic:
    anchor_id: str
    graph_id: str
    status: GraphAnchorStatus


@dataclass(frozen=True)
class GraphContext:
    enabled: bool = True
    status: Literal["ok", "empty", "disabled"] = "empty"
    evidence: tuple[GraphEvidence, ...] = ()
    total_chars: int = 0
    max_chars: int = 6000
    was_truncated: bool = False
    diagnostics: tuple[GraphDiagnostic, ...] = ()


def build_graph_context(result: GraphRetrievalResult, *, max_chars: int = 6000) -> GraphContext:
    """Project successful typed results, then admit complete records in order.

    No retrieval or text-context dependency. The budget counts the exact JSON
    evidence body, including separators; prompt instructions are fixed overhead.
    A full provenance set is retained internally even when graph records are cut.
    Non-success statuses (including repository truncation) are diagnostics only.
    """
    max_chars = max(0, max_chars)
    if not result.enabled:
        return GraphContext(enabled=False, status="disabled", max_chars=max_chars)
    diagnostics = tuple(GraphDiagnostic(item.ref.anchor_id, item.ref.graph_id, item.status)
                        for item in result.items)
    candidates = [candidate for item in result.items if (candidate := _to_evidence(item)) is not None]
    candidates.sort(key=lambda item: (item.ref.graph_id, min(item.source_citations), item.ref.anchor_id))
    kept: list[GraphEvidence] = []
    used_chars = 0
    was_truncated = False
    for candidate in candidates:
        current = replace(candidate, entities=(), relationships=())
        separator = 2 if kept else 0
        remaining = max_chars - used_chars - separator
        if len(_format_evidence(current)) > remaining:
            was_truncated = True
            break
        exhausted = False
        for entity in candidate.entities:
            expanded = replace(current, entities=(*current.entities, entity))
            if len(_format_evidence(expanded)) > remaining:
                exhausted = True
                break
            current = expanded
        if not exhausted:
            ids = {entity.id for entity in current.entities}
            for relationship in candidate.relationships:
                if not {relationship.source_entity_id, relationship.target_entity_id} <= ids:
                    continue
                expanded = replace(current, relationships=(*current.relationships, relationship))
                if len(_format_evidence(expanded)) > remaining:
                    exhausted = True
                    break
                current = expanded
        if exhausted:
            # true is shorter than false in the explicit JSON projection; this
            # flag cannot make an already admitted record exceed its budget.
            current = replace(current, was_truncated=True)
            was_truncated = True
        kept.append(current)
        used_chars += separator + len(_format_evidence(current))
        if exhausted:
            break
    return GraphContext(
        status="ok" if kept else "empty", evidence=tuple(kept),
        total_chars=used_chars, max_chars=max_chars, was_truncated=was_truncated,
        diagnostics=diagnostics,
    )


def format_graph_context_for_prompt(context: GraphContext) -> str:
    if not context.enabled or context.status != "ok":
        return ""
    return "\n\n".join(_format_evidence(item) for item in context.evidence)


def _to_evidence(item: GraphAnchorResult) -> GraphEvidence | None:
    if (item.status != "success" or item.document is None or item.table is None
            or not item.template_name):
        return None
    provenance = []
    for origin in item.provenance:
        if (origin.anchor_id == item.ref.anchor_id and type(origin.citation_id) is int
                and origin.citation_id > 0 and origin.document_id and origin.chunk_id
                and origin not in provenance):
            provenance.append(deepcopy(origin))
    if not provenance:
        return None
    provenance.sort(key=lambda p: (p.citation_id, p.document_id, p.chunk_id))
    return GraphEvidence(item.ref, item.template_name, item.document, item.table,
                         item.entities, item.relationships, tuple(provenance))


def _format_evidence(item: GraphEvidence) -> str:
    # Deliberate projection. Do not dump GraphRetrievalResult, diagnostics, or
    # arbitrary source metadata into the prompt. Public citations stay textual.
    payload = {
        "template": item.template_name,
        "graph_id": item.ref.graph_id,
        "anchor_id": item.ref.anchor_id,
        "anchor_type": item.ref.anchor_type,
        "table_ref": item.ref.table_ref,
        "document": asdict(item.document),
        "table": asdict(item.table),
        "source_citations": list(item.source_citations),
        "entities": [asdict(entity) for entity in item.entities],
        "relationships": [asdict(relationship) for relationship in item.relationships],
        "was_truncated": item.was_truncated,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
