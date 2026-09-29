from __future__ import annotations

import os

import pytest

from phase13_support import schema_engine, verified_engine


@pytest.fixture(scope="session")
def phase13_root_engine():
    url = os.environ.get("PHASE13_TEST_DATABASE_URL")
    cluster = os.environ.get("PHASE13_TEST_CLUSTER", "")
    confirmation = os.environ.get("PHASE13_TEST_CONFIRMED_DATABASE", "")
    if not url:
        pytest.skip("Phase 13 requires an explicitly confirmed isolated PostgreSQL instance")
    engine = verified_engine(url, cluster, confirmation)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def pg_engine(phase13_root_engine):
    engine = schema_engine(phase13_root_engine)
    try:
        yield engine
    finally:
        engine.dispose()
