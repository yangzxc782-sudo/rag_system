// Deployment prerequisite only. Not executed by the application or this milestone.
// Inspect existing constraints/data first; run only after separate authorization.
CREATE CONSTRAINT kg_source_path IF NOT EXISTS
FOR (g:KnowledgeGraph) REQUIRE g.source_path IS UNIQUE;
CREATE CONSTRAINT kg_graph_id IF NOT EXISTS
FOR (g:KnowledgeGraph) REQUIRE g.graph_id IS UNIQUE;
CREATE CONSTRAINT me_graph_entity IF NOT EXISTS
FOR (e:MaterialEntity) REQUIRE (e.graph_id, e.entity_id) IS UNIQUE;
