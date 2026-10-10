"""Shared half-open canonical-source interval predicates; no segmentation policy."""


def overlaps(start: int, end: int, other_start: int, other_end: int) -> bool:
    """Strict intersection; touching endpoints are not an overlap."""
    return max(start, other_start) < min(end, other_end)
