"""Strict source/build-bound KG interval mapping, independent of segmentation."""
from app.extraction.kg_protocol import ANCHOR_ADAPTER
from app.ingestion.source_intervals import overlaps


def anchor_intervals(anchors, *, document_id, source_version, graph_build_id, character_count):
    refs = {}
    for entry in anchors:
        if any(entry[key] != value for key, value in (("document_id", document_id),
                ("source_version", source_version), ("graph_build_id", graph_build_id))):
            raise ValueError("Anchor belongs to another source/build")
        start, end = entry["source_start"], entry["source_end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= character_count:
            raise ValueError("Invalid anchor interval")
        ref = ANCHOR_ADAPTER.validate_python(entry["anchor_metadata"]).business_metadata()
        key, row = (ref["graph_id"], ref["anchor_id"]), (start, end, ref)
        if key in refs and refs[key] != row:
            raise ValueError("Conflicting anchor identity")
        refs[key] = row
    return tuple(sorted(refs.values(), key=lambda row: (row[0], row[1], row[2]["graph_id"], row[2]["anchor_id"])))


def refs_for_interval(start, end, anchors):
    return [dict(ref) for a, b, ref in anchors if overlaps(start, end, a, b)]
