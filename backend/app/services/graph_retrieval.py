"""Verified-source graph reads; no graph construction or legacy query fallback."""
from dataclasses import replace
import time

from app.graph.models import GraphAnchorResult, GraphRetrievalRequest, GraphRetrievalResult, VerifiedAnchor
from app.services.graph_sources import GraphSourceAuthority


class GraphRetrievalService:
    def __init__(self, settings, *, repository, authority=None, clock=None):
        self._settings, self._repository = settings, repository
        self.authority = authority or GraphSourceAuthority(settings)
        self._clock = clock or time.monotonic

    def retrieve(self, anchors: tuple[VerifiedAnchor, ...]) -> GraphRetrievalResult:
        started = self._clock()
        variants = {}
        for a in anchors:
            if not isinstance(a, VerifiedAnchor):
                raise TypeError("Verified anchors required")
            variants.setdefault((a.ref.graph_id, a.ref.anchor_id), []).append(a)
        results, groups, ordered = {}, {}, {}
        selected = 0
        for key, values in variants.items():
            a = values[0]
            conflict = any(v.ref != a.ref or v.source != a.source for v in values)
            origins = tuple(dict.fromkeys(p for v in values for p in v.provenance))
            a = replace(a, provenance=origins)
            ordered[key] = a
            status = ("disabled" if not self._settings.graph_retrieval_enabled else
                      "conflicting_ref" if conflict else
                      "limit_exceeded" if selected >= self._settings.graph_retrieval_max_anchors else None)
            if status:
                results[key] = GraphAnchorResult(a, status)
            else:
                selected += 1
                groups.setdefault(a.ref.graph_id, []).append(a)
        for group in groups.values():
            elapsed = self._clock() - started
            remaining = self._settings.graph_retrieval_total_budget_seconds - elapsed
            if elapsed >= self._settings.graph_retrieval_total_budget_seconds:
                for a in group:
                    results[(a.ref.graph_id, a.ref.anchor_id)] = GraphAnchorResult(a, "budget_exhausted")
                continue
            try:
                request = GraphRetrievalRequest(tuple(group), remaining_budget_ms=remaining * 1000)
                returned = self._repository.fetch_anchor_context(request)
                for a in group:
                    matches = [r for r in returned if isinstance(r, GraphAnchorResult) and r.binding == a]
                    results[(a.ref.graph_id, a.ref.anchor_id)] = (matches[0] if len(matches) == 1 else
                        GraphAnchorResult(a, "unavailable"))
            except Exception:
                for a in group:
                    results[(a.ref.graph_id, a.ref.anchor_id)] = GraphAnchorResult(a, "unavailable")
        return GraphRetrievalResult(tuple(results[key] for key in ordered), self._settings.graph_retrieval_enabled)
