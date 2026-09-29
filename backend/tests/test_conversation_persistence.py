from __future__ import annotations

import ast
import importlib.util
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, configure_mappers
from sqlalchemy.schema import CreateTable

import app.models  # noqa: F401
from app.db.base import Base
from app.schemas.conversation_persistence import PersistenceMetrics
from app.services.conversation_repository import ConversationError, ConversationRepository, fingerprint, _metrics
from phase13_support import validate_test_target


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/0009_phase13_chat_expand.py"


def test_mappers_and_compound_ownership_keys():
    configure_mappers()
    tables = Base.metadata.tables
    for name, parent in [
        ("qa_messages", "qa_turns"), ("qa_turn_artifacts", "qa_turns"),
        ("qa_evidence_snapshots", "qa_turn_artifacts"), ("qa_evidence_sources", "qa_evidence_snapshots"),
        ("retrieval_logs", "qa_messages"),
    ]:
        matches = [c for c in tables[name].constraints if isinstance(c, ForeignKeyConstraint)
                   and c.referred_table.name == parent and "session_id" in c.columns]
        assert matches, name
    assert {fk.referred_table.name for fk in tables["qa_evidence_sources"].foreign_key_constraints} == {"qa_evidence_snapshots"}
    uniques = {tuple(c.columns.keys()) for c in tables["qa_turns"].constraints if isinstance(c, UniqueConstraint)}
    assert {("session_id", "request_id"), ("session_id", "turn_no")} <= uniques
    sql = str(CreateTable(tables["qa_messages"]).compile(dialect=postgresql.dialect()))
    assert "UNIQUE (turn_id, role)" in sql
    assert "UNIQUE (session_id, sequence_no)" in sql


def test_migration_offline_sql_and_no_runtime_model_dependency():
    spec = importlib.util.spec_from_file_location("m1_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    buffer = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        module.upgrade()
    sql = buffer.getvalue()
    assert "row_number() OVER" in sql
    assert "PARTITION BY session_id ORDER BY created_at, id" in sql
    assert "SET turn_id" not in sql
    assert "DELETE FROM" not in sql
    assert "DROP TABLE" not in sql
    assert "qa_evidence_snapshots" in sql
    assert module.down_revision == "0008_phase10_enforce"
    imports = [node for node in ast.walk(ast.parse(MIGRATION.read_text(encoding="utf-8"))) if isinstance(node, ast.ImportFrom)]
    assert not any((node.module or "").startswith("app.") for node in imports)


@pytest.mark.parametrize("value", [
    {"content": "retrieved text"}, {"payload": {"content": "text"}},
    {"usage": {"prompt": "copied evidence"}}, {"latency_ms": "12"},
    {"fallback_code": "long free-form diagnostic containing evidence"},
])
def test_metadata_rejects_text_copies_and_wrong_types(value):
    with pytest.raises(ConversationError, match="typed persistence metrics"):
        _metrics(value)


def test_metrics_accept_only_typed_safe_shape():
    assert _metrics(PersistenceMetrics(latency_ms=2, candidate_count=3, rerank_applied=False)) == {
        "latency_ms": 2, "candidate_count": 3, "rerank_applied": False,
    }


def test_fingerprint_is_order_stable_and_rejects_nonfinite_values():
    assert fingerprint({"a": 1, "b": "问题"}) == fingerprint({"b": "问题", "a": 1})
    assert fingerprint({"limit": 8}) != fingerprint({"limit": 9})
    with pytest.raises(ValueError):
        fingerprint({"score": float("nan")})


def test_repository_requires_caller_transaction_without_opening_connection():
    with Session() as db:
        with pytest.raises(ConversationError, match="caller-owned"):
            ConversationRepository(db).create_session(uuid4())


@pytest.mark.parametrize("url,cluster,confirmation", [
    ("postgresql+psycopg://phase13_m1:x@127.0.0.1:61522/rag_system", "phase13-m1-test-aaaaaaaaaaaa", "rag_system"),
    ("postgresql+psycopg://phase13_m1:x@127.0.0.1:5432/phase13_m1_test_aaaaaaaaaaaa", "phase13-m1-test-aaaaaaaaaaaa", "phase13_m1_test_aaaaaaaaaaaa"),
    ("postgresql+psycopg://phase13_m1:x@remote:61522/phase13_m1_test_aaaaaaaaaaaa", "phase13-m1-test-aaaaaaaaaaaa", "phase13_m1_test_aaaaaaaaaaaa"),
    ("postgresql+psycopg://phase13_m1:x@127.0.0.1:61522/phase13_m1_test_aaaaaaaaaaaa?host=other", "phase13-m1-test-aaaaaaaaaaaa", "phase13_m1_test_aaaaaaaaaaaa"),
    ("postgresql+psycopg://phase13_m1:x@127.0.0.1:61522/phase13_m1_test_aaaaaaaaaaaa", "other", "phase13_m1_test_aaaaaaaaaaaa"),
])
def test_shared_or_unconfirmed_postgres_targets_are_rejected(url, cluster, confirmation):
    with pytest.raises(ValueError, match="non-dedicated"):
        validate_test_target(url, cluster, confirmation)


def test_fresh_isolated_target_passes_static_gate():
    validate_test_target(
        "postgresql+psycopg://phase13_m1:throwaway@127.0.0.1:61522/phase13_m1_test_aaaaaaaaaaaa",
        "phase13-m1-test-aaaaaaaaaaaa", "phase13_m1_test_aaaaaaaaaaaa",
    )


@pytest.mark.parametrize("revision", ["0007_phase10_expand", "0008_phase10_enforce", "0009_phase13_chat_expand"])
def test_document_deletion_runtime_gate_requires_qa_tables(monkeypatch, revision):
    from integration import conftest as gate

    class FakeEngine:
        disposed = False

        def connect(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def scalar(self, _statement):
            return revision

        def dispose(self):
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(gate, "create_engine", lambda *_args, **_kwargs: engine)
    settings = SimpleNamespace(database_url="unused", resource_domain=SimpleNamespace(value="validation"))
    if revision == "0009_phase13_chat_expand":
        assert gate._phase10_domain_engine(settings) is engine
        assert not engine.disposed
    else:
        with pytest.raises(gate.Phase10IntegrationGateError, match="0009_phase13_chat_expand"):
            gate._phase10_domain_engine(settings)
        assert engine.disposed
