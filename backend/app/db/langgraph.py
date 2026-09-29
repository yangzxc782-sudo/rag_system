"""Explicit PostgreSQL checkpoint lifecycle; all DDL belongs to Alembic 0010."""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from importlib.metadata import version
import json
from math import ceil
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.rag.conversation_state import ExecutionIdentity, MAX_STATE_BYTES
from app.services.conversation_repository import ConversationError, ConversationRepository


CHECKPOINT_SCHEMA = "langgraph_checkpoints"
UPSTREAM_SCHEMA_SHA256 = "b61d83ce19b67141d851d7fa29a73ecc6f5cd8e9d45c1aa0f6b6f9ebe50f26bf"
PINNED_VERSIONS = {"langgraph": "1.2.12", "langgraph-checkpoint": "4.2.0",
                   "langgraph-checkpoint-postgres": "3.1.2", "psycopg": "3.3.4", "psycopg-pool": "3.3.3"}


class CheckpointUnavailable(RuntimeError):
    """Safe operational message; never includes connection credentials."""


class CheckpointPool:
    def __init__(self, settings: Settings, *, pool_factory=ConnectionPool):
        self.settings = settings
        self.pool = None
        self._pool_factory = pool_factory

    def open(self) -> None:
        if self.pool is not None:
            return
        if any(version(package) != pin for package, pin in PINNED_VERSIONS.items()):
            raise CheckpointUnavailable("QA_CHECKPOINT_VERSION_MISMATCH: install constraints-phase13.txt")
        actual = sha256("\n".join(BasePostgresSaver.MIGRATIONS).encode()).hexdigest()
        if actual != UPSTREAM_SCHEMA_SHA256:
            raise CheckpointUnavailable("QA_CHECKPOINT_SCHEMA_VERSION_MISMATCH")
        url = make_url(self.settings.database_url)
        if url.drivername != "postgresql+psycopg":
            raise CheckpointUnavailable("QA_CHECKPOINT_CONFIG_INVALID: synchronous psycopg URL required")
        timeout = self.settings.conversation_checkpoint_timeout_seconds
        pool = self._pool_factory(
            conninfo=url.set(drivername="postgresql").render_as_string(hide_password=False),
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0,
                    "connect_timeout": max(1, ceil(timeout)),
                    "options": f"-c search_path={CHECKPOINT_SCHEMA},pg_catalog -c statement_timeout={ceil(timeout * 1000)}"},
            min_size=self.settings.conversation_checkpoint_pool_min_size,
            max_size=self.settings.conversation_checkpoint_pool_max_size,
            timeout=timeout, open=False, check=ConnectionPool.check_connection,
            name="phase13-checkpoints",
        )
        self.pool = pool
        try:
            pool.open(wait=True, timeout=timeout)
            self.health()
        except Exception:
            self.close()
            raise CheckpointUnavailable("QA_CHECKPOINT_NOT_READY: check connection and apply approved 0010 migration") from None

    def health(self) -> None:
        if self.pool is None:
            raise CheckpointUnavailable("QA_CHECKPOINT_CLOSED")
        try:
            with self.pool.connection() as connection:
                versions = connection.execute("SELECT v FROM checkpoint_migrations ORDER BY v").fetchall()
                if [row["v"] for row in versions] != list(range(10)):
                    raise CheckpointUnavailable("QA_CHECKPOINT_SCHEMA_VERSION_MISMATCH")
                connection.execute("SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata FROM checkpoints LIMIT 0")
                connection.execute("SELECT thread_id, checkpoint_ns, channel, version, type, blob FROM checkpoint_blobs LIMIT 0")
                connection.execute("SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob, task_path FROM checkpoint_writes LIMIT 0")
                # The project's Alembic/QA tables live in public, separate from the
                # pool search path. Readiness must not silently adopt a partial DB.
                head = connection.execute("SELECT version_num FROM public.alembic_version").fetchone()
                if head is None or head["version_num"] != "0010_phase13_checkpoints":
                    raise CheckpointUnavailable("QA_CHECKPOINT_MIGRATION_REQUIRED")
        except CheckpointUnavailable:
            raise
        except Exception:
            raise CheckpointUnavailable("QA_CHECKPOINT_NOT_READY: approved 0010 schema required") from None

    def close(self) -> None:
        pool, self.pool = self.pool, None
        if pool is not None:
            pool.close(timeout=self.settings.conversation_checkpoint_timeout_seconds)

    def saver(self, thread_id: UUID, session_factory, *, identity: ExecutionIdentity | None = None):
        if self.pool is None:
            raise CheckpointUnavailable("QA_CHECKPOINT_CLOSED")
        # PostgresSaver owns a lock: create one per graph invocation/read operation,
        # not one global Saver serializing all threads through that lock.
        return ThreadBoundSaver(PostgresSaver(self.pool), thread_id, session_factory, identity=identity)


class ThreadBoundSaver(BaseCheckpointSaver):
    """Upstream persistence with thread binding and a short business write fence.

    QA rows and checkpoint writes use separate transactions. Holding the business
    row lock only across the checkpoint write prevents a superseded attempt from
    becoming latest. Artifact commit/checkpoint failure still needs replay, not a
    fictitious distributed transaction. No locks span LLM calls.
    """
    def __init__(self, delegate, thread_id: UUID, session_factory, *, identity=None):
        super().__init__(serde=delegate.serde)
        self.delegate, self.thread_id = delegate, str(thread_id)
        self.session_factory, self.identity = session_factory, identity

    def _check(self, config):
        values = config.get("configurable", {})
        if values.get("thread_id") != self.thread_id or values.get("checkpoint_ns", "") != "":
            raise ConversationError("QA_CHECKPOINT_THREAD_MISMATCH", "Checkpoint thread/namespace mismatch.", status_code=409)

    @contextmanager
    def _fence(self, config):
        self._check(config)
        if self.identity is None or str(self.identity.thread_id) != self.thread_id:
            raise ConversationError("QA_CHECKPOINT_READ_ONLY", "Checkpoint writes require an execution identity.", status_code=409)
        with self.session_factory() as db, db.begin():
            self.identity.validate(ConversationRepository(db), lock=True)
            yield

    @staticmethod
    def _bounded(value):
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_STATE_BYTES * 4:
            raise ConversationError("QA_CHECKPOINT_TOO_LARGE", "Checkpoint reference payload exceeds its ceiling.", status_code=409)

    def get_tuple(self, config):
        self._check(config)
        return self.delegate.get_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None):
        self._check(config)
        if before is not None:
            self._check(before)
        yield from self.delegate.list(config, filter=filter, before=before, limit=limit)

    def put(self, config, checkpoint, metadata, new_versions):
        self._bounded(checkpoint["channel_values"])
        with self._fence(config):
            return self.delegate.put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        # Exceptions may include external response text. Persist a code only.
        safe_writes = [(channel, "QA_GRAPH_NODE_FAILED" if channel == "__error__" else value)
                       for channel, value in writes]
        self._bounded(safe_writes)
        with self._fence(config):
            self.delegate.put_writes(config, safe_writes, task_id, task_path)

    def get_next_version(self, current, channel):
        return self.delegate.get_next_version(current, channel)
