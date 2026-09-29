"""One physical PostgreSQL session owns the lock, graph writes and publication.

The connection is held for execution; transactions are short. Neither SQLAlchemy
nor the Saver may replace it after disconnect. Different threads borrow different
connections. The RLock only serializes use of this connection by LangGraph's
checkpoint worker and its node worker; it is not the cross-process execution lock.
"""
from contextlib import contextmanager
from hashlib import sha256
from threading import RLock
from uuid import UUID

from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app.db.langgraph import ThreadBoundSaver
from app.services.conversation_repository import ConversationError


LOCK_NAMESPACE = b"rag_system:phase13:thread_execution:v1:"


def thread_lock_key(thread_id: UUID) -> int:
    return int.from_bytes(sha256(LOCK_NAMESPACE + thread_id.bytes).digest()[:8], "big", signed=True)


class ThreadLockLost(ConversationError):
    def __init__(self):
        super().__init__("QA_EXECUTION_LOCK_LOST", "Execution connection was lost; query request status.", status_code=503)


class ThreadExecutionLocks:
    def __init__(self, settings):
        # Dedicated pool prevents search_path and session advisory locks escaping
        # into ordinary application connections. No DDL and no connection at init.
        self.engine = create_engine(settings.database_url, pool_pre_ping=True,
            pool_size=settings.conversation_checkpoint_pool_max_size, max_overflow=0,
            pool_timeout=settings.conversation_checkpoint_timeout_seconds,
            connect_args={"prepare_threshold": 0, "options":
                "-c search_path=langgraph_checkpoints,public,pg_catalog "
                f"-c statement_timeout={int(settings.conversation_checkpoint_timeout_seconds * 1000)}"})

    @contextmanager
    def acquire(self, thread_id: UUID):
        with self.engine.connect() as connection:
            lease = ThreadExecutionLease(connection, thread_id)
            try:
                acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lease.key})
                connection.commit()  # Session lock survives; no idle transaction.
                lease.held = bool(acquired)
                yield lease if acquired else None
            finally:
                lease.release()

    def close(self):
        self.engine.dispose()


class ThreadExecutionLease:
    def __init__(self, connection, thread_id):
        self.connection, self.thread_id = connection, thread_id
        self.raw = connection.connection.driver_connection
        self.backend_pid = self.raw.info.backend_pid
        self.key, self.held = thread_lock_key(thread_id), False
        self.mutex = RLock()

    def assert_alive(self):
        if (not self.held or self.connection.closed or self.connection.invalidated
                or self.raw.closed or self.raw.broken):
            raise ThreadLockLost()

    def session_factory(self):
        # Return an actual Session, including the close() contract used by the
        # frozen Hybrid deletion filter (which does not use a with statement).
        return ExecutionSession(self)

    def saver(self, thread_id, session_factory, *, identity=None):
        if thread_id != self.thread_id:
            raise ConversationError("QA_CHECKPOINT_THREAD_MISMATCH", "Execution thread mismatch.", status_code=409)
        self.assert_alive()
        return ExecutionBoundSaver(PostgresSaver(self.raw), thread_id, self.session_factory, identity=identity)

    def release(self):
        with self.mutex:
            if not self.held:
                # On an uncertain acquisition/commit, invalidate rather than put
                # a potentially locked physical session back in the pool.
                self.connection.invalidate()
                return
            try:
                self.assert_alive()
                self.connection.rollback()
                if not self.connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": self.key}):
                    raise ThreadLockLost()
                self.connection.commit()
            except Exception:
                self.connection.invalidate()
            finally:
                self.held = False


class ExecutionBoundSaver(ThreadBoundSaver):
    """M2's thread/attempt fences, now sharing the execution physical connection.

    Inherited writes already open a session transaction in _fence. Reads also
    explicitly enlist the raw Saver operation so no implicit psycopg transaction
    survives until the next model call. No upstream setup/migration is called.
    """
    def get_tuple(self, config):
        self._check(config)
        with self.session_factory() as db, db.begin():
            db.execute(text("SELECT 1"))
            return self.delegate.get_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None):
        self._check(config)
        if before is not None:
            self._check(before)
        with self.session_factory() as db, db.begin():
            db.execute(text("SELECT 1"))
            rows = list(self.delegate.list(config, filter=filter, before=before, limit=limit))
        yield from rows


class ExecutionSession(Session):
    def __init__(self, lease):
        self.lease, self.released = lease, False
        lease.mutex.acquire()
        try:
            lease.assert_alive()
            super().__init__(bind=lease.connection, autoflush=False, expire_on_commit=False, close_resets_only=False)
        except BaseException:
            lease.mutex.release()
            raise

    def close(self):
        try:
            super().close()
        finally:
            if not self.released:
                self.released = True
                self.lease.mutex.release()


@event.listens_for(ExecutionSession, "do_orm_execute")
def _guard_execute(state):
    state.session.lease.assert_alive()


@event.listens_for(ExecutionSession, "before_flush")
def _guard_flush(session, context, instances):
    session.lease.assert_alive()
