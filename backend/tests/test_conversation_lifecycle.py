from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import langgraph as checkpoint_module
from app.db.langgraph import CheckpointPool, CheckpointUnavailable
from app.main import create_app
from phase13_m2_support import FakeProvider, settings_for


def test_disabled_default_does_not_open_checkpoint_resources(monkeypatch):
    monkeypatch.setattr(CheckpointPool, "open", lambda self: pytest.fail("disabled pool opened"))
    with TestClient(create_app(settings=settings_for())) as client:
        assert client.get("/health").status_code == 200
        assert client.app.state.conversation_graph is None


def runtime_fakes(monkeypatch, *, failure=None, factory_failure=False):
    from app.db import session as session_module
    from app.llm import provider as provider_module
    settings = settings_for(conversation_enabled=True)
    engine = create_engine(settings.database_url)
    monkeypatch.setattr(session_module, "SessionLocal", sessionmaker(bind=engine))
    instances, provider = [], FakeProvider()
    class Pool:
        def __init__(self, settings):
            self.closed = False
            instances.append(self)
        def open(self):
            if failure:
                raise failure
        def close(self):
            self.closed = True
    def build(settings):
        if factory_failure:
            raise RuntimeError("synthetic provider construction failure")
        return provider
    monkeypatch.setattr(checkpoint_module, "CheckpointPool", Pool)
    monkeypatch.setattr(provider_module, "build_llm_provider", build)
    return settings, instances, provider


def test_lifespan_owns_pool_and_provider_and_closes_them(monkeypatch):
    settings, pools, provider = runtime_fakes(monkeypatch)
    app = create_app(settings=settings)
    with TestClient(app):
        assert app.state.conversation_graph is not None
        assert not pools[0].closed and not provider.closed
    assert pools[0].closed and provider.closed
    assert app.state.conversation_graph is None


def test_initialization_failure_closes_pool_and_fails_startup(monkeypatch):
    settings, pools, provider = runtime_fakes(monkeypatch, failure=CheckpointUnavailable("schema missing"))
    with pytest.raises(CheckpointUnavailable):
        with TestClient(create_app(settings=settings)):
            pytest.fail("Startup should fail")
    assert pools[0].closed and not provider.closed


def test_provider_construction_failure_releases_checkpoint_pool(monkeypatch):
    settings, pools, _ = runtime_fakes(monkeypatch, factory_failure=True)
    with pytest.raises(RuntimeError, match="provider construction"):
        with TestClient(create_app(settings=settings)):
            pass
    assert pools[0].closed


def test_runtime_rejects_different_business_and_checkpoint_databases(monkeypatch):
    settings, pools, _ = runtime_fakes(monkeypatch)
    other = settings.model_copy(update={"database_url": settings.database_url + "_different"})
    with pytest.raises(CheckpointUnavailable, match="DATABASE_MISMATCH"):
        with TestClient(create_app(settings=other)):
            pass
    assert not pools


@pytest.mark.parametrize("casting_enabled,graph_version,required", [
    (False, "phase13_m3_v2", "0010"),
    (True, "phase13_m3_v2", "0011"),
    (True, "casting_v1_v3", "0012"),
])
def test_pool_configuration_and_open_failure_resource_ownership(casting_enabled, graph_version, required):
    captured = {}
    class Pool:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.closed = False
        def open(self, **kwargs):
            raise RuntimeError("SECRET_connection_detail")
        def close(self, **kwargs):
            self.closed = True
            captured["closed"] = True
    pool = CheckpointPool(settings_for(conversation_enabled=True,
        casting_design_enabled=casting_enabled, conversation_graph_version=graph_version), pool_factory=Pool)
    with pytest.raises(CheckpointUnavailable) as error:
        pool.open()
    assert "SECRET" not in str(error.value)
    assert str(error.value).startswith("QA_CHECKPOINT_NOT_READY:")
    assert f"{required} or later compatible migration" in str(error.value)
    assert pool.pool is None and captured["closed"]
    assert captured["max_size"] > 1
    assert captured["kwargs"]["autocommit"] is True
    assert captured["kwargs"]["prepare_threshold"] == 0
    assert "search_path=langgraph_checkpoints,pg_catalog" in captured["kwargs"]["options"]


def test_version_drift_is_rejected_before_connection_creation(monkeypatch):
    monkeypatch.setattr(checkpoint_module, "version", lambda _: "0.0.0")
    with pytest.raises(CheckpointUnavailable, match="VERSION_MISMATCH"):
        CheckpointPool(settings_for(), pool_factory=lambda **_: pytest.fail("opened on version drift")).open()


class ReadinessPool:
    """Record readiness SQL without connecting to a database."""
    def __init__(self, revision, *, missing_table=None):
        self.revision = revision
        self.missing_table = missing_table
        self.queries = []
        self.closed = False

    def open(self, **kwargs):
        pass

    def close(self, **kwargs):
        self.closed = True

    @contextmanager
    def connection(self):
        yield self

    def execute(self, query):
        self.queries.append(query)
        if self.missing_table and f"FROM {self.missing_table} " in query:
            raise RuntimeError("SECRET_schema_failure")
        if query == "SELECT v FROM checkpoint_migrations ORDER BY v":
            return SimpleNamespace(fetchall=lambda: [{"v": value} for value in range(10)])
        if query == "SELECT version_num FROM public.alembic_version":
            return SimpleNamespace(fetchone=lambda: (
                {"version_num": self.revision} if self.revision is not None else None))
        assert query.startswith("SELECT ") and query.endswith(" LIMIT 0")


@pytest.mark.parametrize("revision,casting_enabled,graph_version,has_casting_storage", [
    ("0010_phase13_checkpoints", False, "phase13_m3_v2", False),
    ("0011_casting_storage", False, "phase13_m3_v2", True),
    ("0011_casting_storage", True, "phase13_m3_v2", True),
    ("0012_casting_answers", False, "phase13_m3_v2", True),
    ("0012_casting_answers", True, "phase13_m3_v2", True),
    ("0012_casting_answers", True, "casting_v1_v3", True),
    ("0013_pdf_kg_versions", False, "phase13_m3_v2", True),
    ("0013_pdf_kg_versions", True, "phase13_m3_v2", True),
    ("0013_pdf_kg_versions", True, "casting_v1_v3", True),
])
def test_checkpoint_revision_capabilities_and_storage_queries(
    revision, casting_enabled, graph_version, has_casting_storage,
):
    connection_pool = ReadinessPool(revision)
    checkpoints = CheckpointPool(settings_for(conversation_enabled=True,
        casting_design_enabled=casting_enabled, conversation_graph_version=graph_version),
        pool_factory=lambda **_: connection_pool)
    checkpoints.open()
    try:
        assert checkpoints.pool is connection_pool
        for table in ("public.casting_design_files", "public.casting_design_runs", "public.qa_turns"):
            assert any(f"FROM {table} LIMIT 0" in query for query in connection_pool.queries) == has_casting_storage
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            assert any(f"FROM {table} LIMIT 0" in query for query in connection_pool.queries)
    finally:
        checkpoints.close()
    assert connection_pool.closed


@pytest.mark.parametrize("revision,casting_enabled,graph_version,error", [
    (None, False, "phase13_m3_v2", "QA_CHECKPOINT_MIGRATION_REQUIRED"),
    ("9999_unknown", False, "phase13_m3_v2", "QA_CHECKPOINT_MIGRATION_REQUIRED"),
    ("9999_unknown", True, "casting_v1_v3", "QA_CHECKPOINT_MIGRATION_REQUIRED"),
    ("0013_unknown", False, "phase13_m3_v2", "QA_CHECKPOINT_MIGRATION_REQUIRED"),
    ("0010_phase13_checkpoints", True, "phase13_m3_v2", "CASTING_MIGRATION_REQUIRED: apply approved 0011"),
    ("0010_phase13_checkpoints", True, "casting_v1_v3", "CASTING_MIGRATION_REQUIRED: apply approved 0011"),
    ("0011_casting_storage", True, "casting_v1_v3", "CASTING_MIGRATION_REQUIRED: apply approved 0012"),
])
def test_checkpoint_revision_capabilities_reject_missing_old_or_unknown_revision(
    revision, casting_enabled, graph_version, error,
):
    checkpoints = CheckpointPool(settings_for(conversation_enabled=True,
        casting_design_enabled=casting_enabled, conversation_graph_version=graph_version))
    checkpoints.pool = connection_pool = ReadinessPool(revision)
    with pytest.raises(CheckpointUnavailable, match=error):
        checkpoints.health()
    assert not any("FROM public.casting_design_" in query for query in connection_pool.queries)


@pytest.mark.parametrize("casting_enabled,graph_version", [
    (False, "phase13_m3_v2"), (True, "phase13_m3_v2"), (True, "casting_v1_v3"),
])
@pytest.mark.parametrize("missing_table", [
    "public.casting_design_files", "public.casting_design_runs", "public.qa_turns",
])
def test_0013_still_rejects_missing_casting_schema(casting_enabled, graph_version, missing_table):
    connection_pool = ReadinessPool("0013_pdf_kg_versions", missing_table=missing_table)
    checkpoints = CheckpointPool(settings_for(conversation_enabled=True,
        casting_design_enabled=casting_enabled, conversation_graph_version=graph_version),
        pool_factory=lambda **_: connection_pool)
    with pytest.raises(CheckpointUnavailable, match="QA_CHECKPOINT_NOT_READY") as caught:
        checkpoints.open()
    assert "SECRET" not in str(caught.value)
    assert any(f"FROM {missing_table} LIMIT 0" in query for query in connection_pool.queries)
    assert checkpoints.pool is None and connection_pool.closed
