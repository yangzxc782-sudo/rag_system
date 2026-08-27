import json
from neo4j import GraphDatabase


# =========================
# Configuration
# =========================

JSON_FILE = r"C:\Users\32884\Desktop\KG-20260826-001.json"

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "yzy913108"


# =========================
# Neo4j connection
# =========================

driver = GraphDatabase.driver(
    NEO4J_URI,
    auth=(NEO4J_USER, NEO4J_PASSWORD),
)


def create_constraints(session):
    """
    Create uniqueness constraints.
    """

    session.run("""
    CREATE CONSTRAINT graph_id_unique IF NOT EXISTS
    FOR (g:KnowledgeGraph)
    REQUIRE g.graph_id IS UNIQUE
    """)

    session.run("""
    CREATE CONSTRAINT document_id_unique IF NOT EXISTS
    FOR (d:Document)
    REQUIRE d.doc_id IS UNIQUE
    """)

    session.run("""
    CREATE CONSTRAINT table_id_unique IF NOT EXISTS
    FOR (t:Table)
    REQUIRE t.table_id IS UNIQUE
    """)

    session.run("""
    CREATE CONSTRAINT entity_id_unique IF NOT EXISTS
    FOR (e:Entity)
    REQUIRE e.id IS UNIQUE
    """)


def import_graph_metadata(session, data):
    session.run("""
    MERGE (g:KnowledgeGraph {graph_id: $graph_id})
    SET
        g.processed_time = $processed_time,
        g.source_type = $source_type,
        g.table_count = $table_count,
        g.entity_count = $entity_count,
        g.relationship_count = $relationship_count
    """,
    graph_id=data.get("graph_id"),
    processed_time=data.get("processed_time"),
    source_type=data.get("source_type"),
    table_count=data.get("table_count"),
    entity_count=data.get("entity_count"),
    relationship_count=data.get("relationship_count"),
    )


def import_document(session, data):
    session.run("""
    MERGE (d:Document {doc_id: $doc_id})
    SET
        d.source_pdf = $source_pdf,
        d.source_type = $source_type

    WITH d

    MATCH (g:KnowledgeGraph {graph_id: $graph_id})
    MERGE (g)-[:HAS_DOCUMENT]->(d)
    """,
    graph_id=data.get("graph_id"),
    doc_id=data.get("doc_id"),
    source_pdf=data.get("source_pdf"),
    source_type=data.get("source_type"),
    )


def import_tables(session, data):
    doc_id = data.get("doc_id")

    for table in data.get("tables", []):

        table_ref = table.get("table_ref")

        # 防止不同文档出现相同 T-P8-1
        table_id = f"{doc_id}_{table_ref}"

        session.run("""
        MERGE (t:Table {table_id: $table_id})
        SET
            t.table_ref = $table_ref,
            t.page = $page,
            t.table_index = $table_index

        WITH t

        MATCH (d:Document {doc_id: $doc_id})
        MERGE (d)-[:HAS_TABLE]->(t)
        """,
        table_id=table_id,
        table_ref=table_ref,
        page=table.get("page"),
        table_index=table.get("index"),
        doc_id=doc_id,
        )


def import_entities(session, data):

    doc_id = data.get("doc_id")

    for entity in data.get("entities", []):

        entity_id = entity.get("id")
        entity_type = entity.get("type")
        entity_name = entity.get("name")

        properties = entity.get("properties", {})

        table_ref = properties.get("表格编号")
        page = properties.get("页码")

        # 创建实体
        session.run("""
        MERGE (e:Entity {id: $entity_id})
        SET
            e.name = $name,
            e.entity_type = $entity_type,
            e.doc_id = $doc_id,
            e.table_ref = $table_ref,
            e.page = $page
        """,
        entity_id=entity_id,
        name=entity_name,
        entity_type=entity_type,
        doc_id=doc_id,
        table_ref=table_ref,
        page=page,
        )

        # Entity -> Table
        if table_ref:

            table_id = f"{doc_id}_{table_ref}"

            session.run("""
            MATCH (t:Table {table_id: $table_id})
            MATCH (e:Entity {id: $entity_id})

            MERGE (t)-[:CONTAINS_ENTITY]->(e)
            """,
            table_id=table_id,
            entity_id=entity_id,
            )
def import_relationships(session, data):
    """
    Import knowledge relationships between Entity nodes.

    JSON format:
    {
        "source_id": "...",
        "target_id": "...",
        "type": "含有",
        "properties": {}
    }
    """

    relationships = data.get("relationships", [])

    imported = 0
    skipped = 0

    for relationship in relationships:

        source_id = relationship.get("source_id")
        target_id = relationship.get("target_id")
        relation_type = relationship.get("type")
        properties = relationship.get("properties", {})

        # -------------------------
        # Basic validation
        # -------------------------

        if not source_id or not target_id or not relation_type:
            print(
                "[SKIP] Invalid relationship:",
                relationship
            )
            skipped += 1
            continue

        relation_type = relation_type.strip()

        # Relationship type must be inserted into Cypher syntax,
        # so reject backticks to avoid malformed Cypher.
        if "`" in relation_type:
            print(
                f"[SKIP] Invalid relationship type: {relation_type}"
            )
            skipped += 1
            continue

        # -------------------------
        # Check endpoint existence
        # -------------------------

        result = session.run(
            """
            MATCH (s:Entity {id: $source_id})
            MATCH (t:Entity {id: $target_id})
            RETURN
                s.id AS source_id,
                t.id AS target_id
            """,
            source_id=source_id,
            target_id=target_id,
        ).single()

        if result is None:
            print(
                f"[SKIP] Entity not found: "
                f"{source_id} --[{relation_type}]--> {target_id}"
            )
            skipped += 1
            continue

        # -------------------------
        # Create relationship
        # -------------------------

        query = f"""
        MATCH (s:Entity {{id: $source_id}})
        MATCH (t:Entity {{id: $target_id}})

        MERGE (s)-[r:`{relation_type}`]->(t)

        SET r += $properties
        SET
            r.graph_id = $graph_id,
            r.doc_id = $doc_id
        """

        session.run(
            query,
            source_id=source_id,
            target_id=target_id,
            properties=properties,
            graph_id=data.get("graph_id"),
            doc_id=data.get("doc_id"),
        )

        imported += 1

    print(
        f"Knowledge relationships imported: {imported}, "
        f"skipped: {skipped}"
    )

def main():

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    with driver.session() as session:

        print("Creating constraints...")
        create_constraints(session)

        print("Importing graph metadata...")
        import_graph_metadata(session, data)

        print("Importing document...")
        import_document(session, data)

        print("Importing tables...")
        import_tables(session, data)

        print("Importing entities...")
        import_entities(session, data)

        print("Importing knowledge relationships...")
        import_relationships(session, data)

    driver.close()

    print("Import completed.")


if __name__ == "__main__":
    main()