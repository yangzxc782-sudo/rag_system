"""Transaction doubles; real Neo4j syntax, timeout and isolation are not claimed."""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.extraction import kg_writer as writer
from app.extraction.kg_extract import qualify
from test_kg_v2_protocol_units import anchor, raw_part


def payload():
    part = qualify(raw_part(), anchor("clause"), 0)
    return dict(graph_schema_version=2, graph_id=anchor()["graph_id"], graph_build_id=str(uuid4()),
        document_id=str(uuid4()), source_version=str(uuid4()), source_path="synthetic/unique/build",
        template_version="4.0.0", character_count=20,
        entities=part["entities"], relationships=part["relationships"],
        units=[dict(anchor=anchor("clause"), source_start=0, source_end=20, eligible=True)])


class Cursor(list):
    def single(self, strict=False):
        assert strict and len(self) == 1
        return self[0]


class Session:
    def __init__(self):
        self.graph = None
        self.entities, self.relationships, self.calls = [], [], []
        self.fail_at = None
        self.constraints = [dict(type="UNIQUENESS", labelsOrTypes=[label], properties=properties) for label, properties in (
            ("KnowledgeGraph", ["graph_id"]), ("KnowledgeGraph", ["source_path"]), ("MaterialEntity", ["graph_id", "entity_id"]))]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def run(self, query):
        assert query.text == writer.CONSTRAINTS and query.timeout > 0
        return self.constraints

    def begin_transaction(self, timeout):
        assert timeout > 0
        return Transaction(self)


class Transaction:
    def __init__(self, session):
        self.session = session
        self.graph = deepcopy(session.graph)
        self.entities, self.relationships = deepcopy(session.entities), deepcopy(session.relationships)
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass  # No commit on error: copies are discarded.

    def run(self, query, **params):
        self.session.calls.append((query, params))
        if query == self.session.fail_at:
            raise TimeoutError("synthetic timeout")
        if query == writer.CLAIM:
            if self.graph is None:
                self.graph = {**params, "template": params["template_version"], "publication_status": "staging"}
                del self.graph["template_version"]
            return Cursor([{"graph": deepcopy(self.graph)}])
        if query == writer.ORPHANS:
            return Cursor([{"count": len(self.entities)}])
        if query in {writer.ENTITIES, writer.RELATIONSHIPS}:
            rows = params["rows"]
            (self.entities if query == writer.ENTITIES else self.relationships).extend(deepcopy(rows))
            return Cursor([{"count": len(rows)}])
        if query == writer.COUNTS:
            return Cursor([dict(entities=len(self.entities), relationships=len(self.relationships),
                                owned_relationships=len(self.relationships))])
        if query == writer.SEAL:
            self.graph["publication_status"] = "built"
            return Cursor([{"count": 1}])
        raise AssertionError("unapproved query")

    def commit(self):
        self.session.graph, self.session.entities, self.session.relationships = self.graph, self.entities, self.relationships
        self.committed = True


class Driver:
    def __init__(self):
        self.value = Session()

    def session(self, *, database):
        assert database == "synthetic"
        return self.value


def test_atomic_transaction_and_idempotent_receipt_never_deletes_or_duplicates():
    data, driver = payload(), Driver()
    gateway = writer.AtomicGraphWriter(driver, database="synthetic")
    first = gateway.write(data)
    assert first["entities"] == 2 and first["relationships"] == 1
    assert gateway.write(data) == first
    assert len(driver.value.entities) == 2 and len(driver.value.relationships) == 1
    assert all("table_ref" not in e["properties_json"] for e in driver.value.entities)
    for query, params in driver.value.calls:
        assert "DELETE" not in query and "HAS_TRIPLE" not in query and "Document" not in query
        assert data["graph_id"] not in query
        assert params["graph_id"] == data["graph_id"]


@pytest.mark.parametrize("query", [writer.ENTITIES, writer.RELATIONSHIPS, writer.COUNTS, writer.SEAL])
def test_failure_at_any_batch_or_seal_rolls_back_entire_graph(query):
    driver = Driver()
    driver.value.fail_at = query
    with pytest.raises(TimeoutError):
        writer.AtomicGraphWriter(driver, database="synthetic").write(payload())
    assert driver.value.graph is None and driver.value.entities == [] and driver.value.relationships == []


@pytest.mark.parametrize("field,value", [("document_id", str(uuid4())), ("source_version", str(uuid4())),
    ("graph_build_id", str(uuid4())), ("source_path", "other/path"), ("payload_sha256", "0" * 64)])
def test_existing_graph_identity_mismatch_never_overwrites(field, value):
    driver, data = Driver(), payload()
    gateway = writer.AtomicGraphWriter(driver, database="synthetic")
    gateway.write(data)
    driver.value.graph[field] = value
    before = deepcopy(driver.value.graph)
    with pytest.raises(ValueError, match="conflict"):
        gateway.write(data)
    assert driver.value.graph == before


def test_missing_constraints_is_error_not_implicit_ddl():
    driver = Driver()
    driver.value.constraints.pop()
    with pytest.raises(ValueError, match="constraints"):
        writer.AtomicGraphWriter(driver, database="synthetic").write(payload())
    assert not driver.value.calls


@pytest.mark.parametrize("mutation", ["graph", "anchor", "endpoint", "relation", "range", "eligibility", "duplicate"])
def test_cross_graph_or_anchor_and_invalid_payload_refuse_before_io(mutation):
    data, driver = payload(), Driver()
    if mutation == "graph":
        data["entities"][0]["properties"]["graph_id"] = "foreign"
    elif mutation == "anchor":
        data["relationships"][0]["properties"]["anchor_id"] = "foreign"
    elif mutation == "endpoint":
        data["relationships"][0]["target_id"] = "foreign"
    elif mutation == "relation":
        data["relationships"][0]["type"] = "具有"
    elif mutation == "range":
        data["units"][0]["source_end"] = 99
    elif mutation == "eligibility":
        data["units"][0]["eligible"] = False
    else:
        data["entities"].append(deepcopy(data["entities"][0]))
    with pytest.raises(ValueError):
        writer.AtomicGraphWriter(driver, database="synthetic").write(data)
    assert not driver.value.calls


def test_size_budget_and_new_schema_labels():
    with pytest.raises(ValueError, match="budget"):
        writer.validate_payload(payload(), max_entities=1)
    assert "HAS_ENTITY" in writer.ENTITIES and "MaterialEntity" in writer.ENTITIES
    assert "RELATES_TO" in writer.RELATIONSHIPS and "relation_type:row.type" in writer.RELATIONSHIPS
