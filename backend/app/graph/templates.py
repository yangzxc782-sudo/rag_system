"""Only fixed read templates; relation types come from the pinned KG template."""

ANCHOR_CONTEXT_V2 = """
UNWIND $anchors AS anchor
CALL {
  WITH anchor
  MATCH (g:KnowledgeGraph {graph_id:$graph_id, document_id:$document_id,
    source_version:$source_version, graph_build_id:$graph_build_id,
    source_path:$source_path, payload_sha256:$payload_sha256,
    graph_schema_version:2, publication_status:'built', template:$template_version})
  RETURN collect(g)[0..2] AS graphs
}
WITH anchor, graphs, head(graphs) AS g
CALL {
  WITH anchor, g
  MATCH (g)-[:HAS_ENTITY]->(e:MaterialEntity)
  WHERE e.graph_id=$graph_id AND e.graph_build_id=$graph_build_id
    AND e.anchor_id=anchor.anchor_id AND e.anchor_type=anchor.anchor_type
    AND e.entity_type IN $entity_types
  WITH DISTINCT e ORDER BY e.entity_id LIMIT $entity_fetch_limit
  RETURN collect(properties(e)) AS entities
}
CALL {
  WITH anchor, g
  MATCH (g)-[:HAS_ENTITY]->(a:MaterialEntity)-[r:RELATES_TO]->(b:MaterialEntity)<-[:HAS_ENTITY]-(g)
  WHERE a.graph_id=$graph_id AND b.graph_id=$graph_id AND r.graph_id=$graph_id
    AND a.graph_build_id=$graph_build_id AND b.graph_build_id=$graph_build_id AND r.graph_build_id=$graph_build_id
    AND a.anchor_id=anchor.anchor_id AND b.anchor_id=anchor.anchor_id AND r.anchor_id=anchor.anchor_id
    AND a.anchor_type=anchor.anchor_type AND b.anchor_type=anchor.anchor_type
    AND a.entity_type IN $entity_types AND b.entity_type IN $entity_types
    AND any(spec IN $relation_specs WHERE spec.type=r.relation_type
      AND ('*' IN spec.from OR a.entity_type IN spec.from)
      AND ('*' IN spec.to OR b.entity_type IN spec.to))
  WITH DISTINCT r, a, b ORDER BY r.rel_id LIMIT $relationship_fetch_limit
  RETURN collect(r{.*, source_entity_id:a.entity_id, target_entity_id:b.entity_id}) AS relationships
}
RETURN anchor.anchor_id AS anchor_id, size(graphs) AS candidate_count,
       properties(g) AS graph, entities, relationships
"""


def resolve_template(name: str) -> str:
    if name != "anchor_context_v2":
        raise ValueError("Unsupported graph query template")
    return ANCHOR_CONTEXT_V2
