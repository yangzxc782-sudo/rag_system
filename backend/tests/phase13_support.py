"""Fail-closed, isolated-instance-only helpers. Never load application Settings."""
from __future__ import annotations

from pathlib import Path
import re
from uuid import uuid4

from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url


BACKEND = Path(__file__).resolve().parents[1]
BASELINE = "0008_phase10_enforce"
HEAD = "0009_phase13_chat_expand"


def validate_test_target(url: str, cluster: str, confirmation: str) -> None:
    target = make_url(url)
    match = re.fullmatch(r"phase13_m1_test_([a-f0-9]{12})", target.database or "")
    if (target.drivername != "postgresql+psycopg" or target.host != "127.0.0.1"
            or target.port is None or not 1024 < target.port < 65536 or target.port in {5432, 5433}
            or target.username != "phase13_m1" or not match or target.query
            or cluster != f"phase13-m1-test-{match.group(1)}" or confirmation != target.database):
        raise ValueError("Refusing a non-dedicated Phase 13 PostgreSQL test target")


def verified_engine(url: str, cluster: str, confirmation: str) -> Engine:
    validate_test_target(url, cluster, confirmation)
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            identity = connection.execute(text(
                "SELECT current_database(), current_user, current_setting('cluster_name')"
            )).one()
            if tuple(identity) != (confirmation, "phase13_m1", cluster):
                raise ValueError("Phase 13 database identity does not match its isolated instance")
    except Exception:
        engine.dispose()
        raise
    return engine


def migration(connection: Connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config()
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    script = ScriptDirectory.from_config(config)
    # Same revision traversal used by Alembic's command layer, but bind ONLY the
    # verified test connection, never env.py or the application's .env URL.
    steps = script._downgrade_revs if downgrade else script._upgrade_revs
    with EnvironmentContext(config, script, fn=lambda rev, _ctx: steps(revision, rev)) as context:
        context.configure(connection=connection)
        context.run_migrations()


def schema_engine(root: Engine, *, revision: str = HEAD) -> Engine:
    namespace = "phase13_m1_" + uuid4().hex
    with root.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{namespace}"')
    engine = create_engine(root.url, connect_args={"options": f"-c search_path={namespace},public"})
    with engine.begin() as connection:
        migration(connection, revision)
    # Schemas/fixtures are deliberately retained for inspection, never dropped.
    return engine
