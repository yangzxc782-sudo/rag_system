"""V2 graph contracts: source ownership comes from SQL, not Neo4j guesses."""
from dataclasses import dataclass, field
from typing import Any, Literal
from app.extraction.kg_protocol import Anchor

GraphAnchorStatus = Literal["success", "not_found", "ambiguous", "conflicting_ref", "timeout",
    "unavailable", "truncated", "disabled", "limit_exceeded", "budget_exhausted"]


@dataclass(frozen=True)
class GraphSource:
    document_id: str
    source_version: str
    graph_build_id: str
    unit_id: str
    source_start: int
    source_end: int
    canonical_sha256: str
    payload_sha256: str
    source_path: str
    template_version: str


@dataclass(frozen=True)
class GraphTriggerProvenance:
    anchor_id: str
    document_id: str
    chunk_id: str
    citation_id: int
    source_version: str
    graph_build_id: str
    chunk_set_id: str
    effective_start: int
    effective_end: int


@dataclass(frozen=True)
class VerifiedAnchor:
    ref: Anchor
    source: GraphSource
    provenance: tuple[GraphTriggerProvenance, ...]

    @property
    def fully_covered(self) -> bool:
        # Temporary coverage check over retained chunks, not a multi-span source model.
        if any(p.document_id != self.source.document_id or p.source_version != self.source.source_version
               or p.graph_build_id != self.source.graph_build_id or p.anchor_id != self.ref.anchor_id
               or type(p.effective_start) is not int or type(p.effective_end) is not int
               or p.effective_start >= p.effective_end or type(p.citation_id) is not int
               or p.citation_id < 1 for p in self.provenance):
            return False
        cursor = self.source.source_start
        for p in sorted(self.provenance, key=lambda p: (p.effective_start, p.effective_end)):
            if p.effective_end <= cursor:
                continue
            if p.effective_start > cursor:
                return False  # Never bridge a gap with a min/max envelope.
            cursor = p.effective_end
            if cursor >= self.source.source_end:
                return True
        return False


@dataclass(frozen=True)
class GraphRetrievalRequest:
    anchors: tuple[VerifiedAnchor, ...]
    # Internal observation only; never changes the query timeout or admission.
    remaining_budget_ms: float | None = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        if not self.anchors:
            raise ValueError("A verified graph group is required")
        def owner(a):
            s = a.source
            return (a.ref.graph_id, s.document_id, s.source_version, s.graph_build_id,
                    s.payload_sha256, s.source_path, s.template_version)
        if (any(owner(a) != owner(self.anchors[0]) for a in self.anchors)
                or len({a.ref.anchor_id for a in self.anchors}) != len(self.anchors)):
            raise ValueError("Conflicting graph ownership")


@dataclass(frozen=True)
class GraphEntity:
    id: str
    name: str
    entity_type: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphRelationship:
    id: str
    type: str
    source_entity_id: str
    target_entity_id: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphAnchorResult:
    binding: VerifiedAnchor
    status: GraphAnchorStatus
    entities: tuple[GraphEntity, ...] = ()
    relationships: tuple[GraphRelationship, ...] = ()
    template_name: str = "anchor_context_v2"


@dataclass(frozen=True)
class GraphRetrievalResult:
    items: tuple[GraphAnchorResult, ...] = ()
    enabled: bool = True
