from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class KGRef:
    """One stable Source anchor reference; contains no chunk identity."""

    anchor_id: str
    graph_id: str
    anchor_type: str
    table_ref: str | None = None


GraphAnchorStatus = Literal[
    "success", "not_found", "ambiguous", "unsupported_anchor_type", "invalid_ref",
    "conflicting_ref", "timeout", "unavailable", "truncated", "disabled",
    "limit_exceeded", "budget_exhausted",
]


@dataclass(frozen=True, slots=True)
class GraphTriggerProvenance:
    """Ephemeral retrieval provenance, never a persisted chunk-to-graph link."""

    anchor_id: str
    document_id: str
    chunk_id: str
    citation_id: int | None = None
    source_range: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class GraphRetrievalRequest:
    graph_id: str
    anchors: tuple[KGRef, ...]
    provenance: tuple[GraphTriggerProvenance, ...] = ()

    def __post_init__(self) -> None:
        if any(anchor.graph_id != self.graph_id for anchor in self.anchors):
            raise ValueError("Graph request must contain a single graph identity")
        if len({anchor.anchor_id for anchor in self.anchors}) != len(self.anchors):
            raise ValueError("Graph request anchors must be unique")


@dataclass(frozen=True, slots=True)
class GraphDocument:
    doc_id: str
    source_pdf: str | None
    source_type: str | None


@dataclass(frozen=True, slots=True)
class GraphTable:
    table_id: str
    table_ref: str
    page: int | None
    table_index: int | None


@dataclass(frozen=True, slots=True)
class GraphEntity:
    id: str
    name: str | None
    entity_type: str | None
    doc_id: str
    table_ref: str
    page: int | None


@dataclass(frozen=True, slots=True)
class GraphRelationship:
    type: str
    source_entity_id: str
    target_entity_id: str
    graph_id: str
    doc_id: str


@dataclass(frozen=True, slots=True)
class GraphTruncation:
    entities: bool = False
    relationships: bool = False
    relationships_limited_by_entities: bool = False


@dataclass(frozen=True, slots=True)
class GraphAnchorResult:
    ref: KGRef
    status: GraphAnchorStatus
    document: GraphDocument | None = None
    table: GraphTable | None = None
    entities: tuple[GraphEntity, ...] = ()
    relationships: tuple[GraphRelationship, ...] = ()
    truncation: GraphTruncation = field(default_factory=GraphTruncation)
    provenance: tuple[GraphTriggerProvenance, ...] = ()
    template_name: str | None = None
    conflicting_refs: tuple[KGRef, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphRetrievalResult:
    items: tuple[GraphAnchorResult, ...] = ()
    enabled: bool = True


def unsupported_ref_status(ref: KGRef) -> GraphAnchorStatus | None:
    """Validate scalars without rewriting stable source identities."""
    if any(not isinstance(value, str) or not value.strip()
           for value in (ref.anchor_id, ref.graph_id, ref.anchor_type)):
        return "invalid_ref"
    if ref.table_ref is not None and not isinstance(ref.table_ref, str):
        return "invalid_ref"
    if ref.anchor_type != "table":
        return "unsupported_anchor_type"
    if not ref.table_ref or not ref.table_ref.strip():
        return "invalid_ref"
    return None
