"""Fixed read-only Cypher for the audited graph schema. No query fragments as input."""

from typing import Literal


RELATIONSHIP_TYPES = ("含有", "规定", "适用于", "约束", "替代", "对应")

# Start at the graph unique key. Candidate pairs are deduplicated before LIMIT
# so duplicate structural edges do not manufacture ambiguity. Aggregating CALL
# subqueries retain one row even when their scoped MATCH yields no results.
TABLE_CONTEXT_V1 = """
MATCH (g:KnowledgeGraph {graph_id: $graph_id})
UNWIND $anchors AS ref
CALL (g, ref) {
    MATCH (g)-[:HAS_DOCUMENT]->(d:Document)-[:HAS_TABLE]->(t:Table {table_ref: ref.table_ref})
    WITH DISTINCT d, t ORDER BY d.doc_id, t.table_id LIMIT 2
    RETURN collect({document: d, table: t}) AS candidates
}
WITH g, ref, candidates,
     CASE WHEN size(candidates) = 1 THEN candidates[0].document ELSE null END AS d,
     CASE WHEN size(candidates) = 1 THEN candidates[0].table ELSE null END AS t
WITH g, ref, candidates, d, t,
     EXISTS { MATCH (other:KnowledgeGraph)-[:HAS_DOCUMENT]->(d) WHERE other <> g } AS shared
CALL (d, t, shared) {
    WITH d, t WHERE d IS NOT NULL AND t IS NOT NULL AND NOT shared
    MATCH (t)-[:CONTAINS_ENTITY]->(e:Entity)
    WHERE e.doc_id = d.doc_id AND e.table_ref = t.table_ref AND e.id IS NOT NULL
    WITH DISTINCT e ORDER BY e.id LIMIT $entity_fetch_limit
    RETURN collect(e { .id, .name, .entity_type, .doc_id, .table_ref, .page }) AS entities
}
CALL (d, t, shared, entities) {
    WITH d, t, entities WHERE d IS NOT NULL AND t IS NOT NULL AND NOT shared
    MATCH (t)-[:CONTAINS_ENTITY]->(s:Entity)
          -[r:`含有`|`规定`|`适用于`|`约束`|`替代`|`对应`]->(target:Entity)
    WHERE s.doc_id = d.doc_id AND target.doc_id = d.doc_id
      AND s.table_ref = t.table_ref AND target.table_ref = t.table_ref
      AND r.graph_id = $graph_id AND r.doc_id = d.doc_id
      AND EXISTS { MATCH (t)-[:CONTAINS_ENTITY]->(target) }
      AND s.id IN [e IN entities[0..$max_entities] | e.id]
      AND target.id IN [e IN entities[0..$max_entities] | e.id]
    WITH DISTINCT s.id AS source_entity_id, type(r) AS relation_type,
         target.id AS target_entity_id, r.graph_id AS graph_id, r.doc_id AS doc_id
    ORDER BY source_entity_id, relation_type, target_entity_id
    LIMIT $relationship_fetch_limit
    RETURN collect({type: relation_type, source_entity_id: source_entity_id,
                    target_entity_id: target_entity_id, graph_id: graph_id, doc_id: doc_id}) AS relationships
}
RETURN ref.anchor_id AS anchor_id, g.graph_id AS graph_id,
       size(candidates) AS candidate_count, shared AS shared_document,
       d { .doc_id, .source_pdf, .source_type } AS document,
       t { .table_id, .table_ref, .page, .table_index } AS table,
       entities, relationships
""".strip()


def resolve_template(name: Literal["table_context_v1"]) -> str:
    if name != "table_context_v1":
        raise ValueError("Unsupported graph template")
    return TABLE_CONTEXT_V1
