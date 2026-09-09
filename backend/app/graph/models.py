from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KGRef:
    """One stable Source anchor reference; contains no chunk identity."""

    anchor_id: str
    graph_id: str
    anchor_type: str
    table_ref: str | None = None
