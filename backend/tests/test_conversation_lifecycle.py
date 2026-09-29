from __future__ import annotations

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


def test_pool_configuration_and_open_failure_resource_ownership():
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
    pool = CheckpointPool(settings_for(), pool_factory=Pool)
    with pytest.raises(CheckpointUnavailable) as error:
        pool.open()
    assert "SECRET" not in str(error.value)
    assert pool.pool is None and captured["closed"]
    assert captured["max_size"] > 1
    assert captured["kwargs"]["autocommit"] is True
    assert captured["kwargs"]["prepare_threshold"] == 0
    assert "search_path=langgraph_checkpoints,pg_catalog" in captured["kwargs"]["options"]


def test_version_drift_is_rejected_before_connection_creation(monkeypatch):
    monkeypatch.setattr(checkpoint_module, "version", lambda _: "0.0.0")
    with pytest.raises(CheckpointUnavailable, match="VERSION_MISMATCH"):
        CheckpointPool(settings_for(), pool_factory=lambda **_: pytest.fail("opened on version drift")).open()
