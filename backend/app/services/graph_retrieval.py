from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import replace
import time
from typing import Protocol

from app.core.config import Settings
from app.graph.models import (
    GraphAnchorResult, GraphRetrievalRequest, GraphRetrievalResult,
    GraphTriggerProvenance, KGRef, unsupported_ref_status,
)


class GraphRepository(Protocol):
    def fetch_table_context(self, request: GraphRetrievalRequest) -> tuple[GraphAnchorResult, ...]: ...


class GraphRetrievalService:
    """Group source references without Hybrid/RAG dependencies or graph writes.

    First occurrence defines canonical order (the caller's M2 source ordering).
    Inspect all duplicates before applying query limits. Clock budget controls
    scheduling of the next graph group, not preemption of an active driver call.
    The caller owns the repository lifecycle.
    """

    def __init__(self, settings: Settings, *, repository: GraphRepository,
                 clock: Callable[[], float] | None = None):
        self._settings = settings
        self._repository = repository
        self._clock = clock or time.monotonic

    def retrieve(self, refs: list[KGRef], *,
                 provenance: Sequence[GraphTriggerProvenance] = ()) -> GraphRetrievalResult:
        started = self._clock()
        variants: dict[str, list[KGRef]] = {}
        for ref in refs:
            if not isinstance(ref, KGRef):
                raise TypeError("Graph retrieval requires KGRef values")
            values = variants.setdefault(ref.anchor_id, [])
            if ref not in values:
                values.append(ref)
        origins = []
        for item in provenance:
            if item.anchor_id in variants and item not in origins:
                origins.append(deepcopy(item))
        results: dict[str, GraphAnchorResult] = {}
        groups: dict[str, list[KGRef]] = {}
        selected = 0
        for anchor_id, values in variants.items():
            ref = values[0]
            if not self._settings.graph_retrieval_enabled:
                status = "disabled"
            elif len(values) > 1:
                # All variants remain diagnostic data; none is selected to query.
                results[anchor_id] = GraphAnchorResult(ref=ref, status="conflicting_ref", conflicting_refs=tuple(values))
                continue
            else:
                status = unsupported_ref_status(ref)
                if status is None and selected >= self._settings.graph_retrieval_max_anchors:
                    status = "limit_exceeded"
            if status is not None:
                results[anchor_id] = GraphAnchorResult(ref=ref, status=status)
            else:
                selected += 1
                groups.setdefault(ref.graph_id, []).append(ref)
        exhausted = False
        for graph_id, anchors in groups.items():
            if exhausted or self._clock() - started >= self._settings.graph_retrieval_total_budget_seconds:
                exhausted = True
                results.update({ref.anchor_id: GraphAnchorResult(ref=ref, status="budget_exhausted") for ref in anchors})
                continue
            request = GraphRetrievalRequest(graph_id, tuple(anchors), tuple(
                item for item in origins if item.anchor_id in {ref.anchor_id for ref in anchors}
            ))
            try:
                returned = self._repository.fetch_table_context(request)
                for ref in anchors:
                    matches = [item for item in returned if isinstance(item, GraphAnchorResult) and item.ref == ref]
                    results[ref.anchor_id] = matches[0] if len(matches) == 1 else GraphAnchorResult(ref=ref, status="unavailable")
            except Exception:
                results.update({ref.anchor_id: GraphAnchorResult(ref=ref, status="unavailable") for ref in anchors})
        return GraphRetrievalResult(items=tuple(replace(results[anchor_id], provenance=tuple(
            item for item in origins if item.anchor_id == anchor_id
        )) for anchor_id in variants), enabled=self._settings.graph_retrieval_enabled)
