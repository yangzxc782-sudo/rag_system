"""Frozen langgraph-checkpoint-postgres 3.1.2 schema; no runtime setup.

Revision ID: 0010_phase13_checkpoints
Revises: 0009_phase13_chat_expand
Upstream BasePostgresSaver.MIGRATIONS (10 entries), joined with newlines, SHA256:
b61d83ce19b67141d851d7fa29a73ecc6f5cd8e9d45c1aa0f6b6f9ebe50f26bf
New empty tables use ordinary indexes inside Alembic's atomic transaction;
upstream CONCURRENTLY is unnecessary here. No application imports or QA updates.
"""
from alembic import op

# Alembic's existing version_num is VARCHAR(32); the filename can be longer.
revision = "0010_phase13_checkpoints"
down_revision = "0009_phase13_chat_expand"
branch_labels = None
depends_on = None

UPSTREAM_MIGRATIONS_SHA256 = "b61d83ce19b67141d851d7fa29a73ecc6f5cd8e9d45c1aa0f6b6f9ebe50f26bf"


def upgrade() -> None:
    # Deliberately refuse an existing namespace, rather than adopt unknown tables.
    op.execute("""
        CREATE SCHEMA langgraph_checkpoints;
        REVOKE ALL ON SCHEMA langgraph_checkpoints FROM PUBLIC;
        CREATE TABLE langgraph_checkpoints.checkpoint_migrations (v INTEGER PRIMARY KEY);
        CREATE TABLE langgraph_checkpoints.checkpoints (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            parent_checkpoint_id TEXT,
            type TEXT,
            checkpoint JSONB NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}',
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        );
        CREATE TABLE langgraph_checkpoints.checkpoint_blobs (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            version TEXT NOT NULL,
            type TEXT NOT NULL,
            blob BYTEA,
            PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
        );
        CREATE TABLE langgraph_checkpoints.checkpoint_writes (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            channel TEXT NOT NULL,
            type TEXT,
            blob BYTEA NOT NULL,
            task_path TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        );
        CREATE INDEX checkpoints_thread_id_idx ON langgraph_checkpoints.checkpoints(thread_id);
        CREATE INDEX checkpoint_blobs_thread_id_idx ON langgraph_checkpoints.checkpoint_blobs(thread_id);
        CREATE INDEX checkpoint_writes_thread_id_idx ON langgraph_checkpoints.checkpoint_writes(thread_id);
        INSERT INTO langgraph_checkpoints.checkpoint_migrations(v) SELECT generate_series(0,9);
    """)


def downgrade() -> None:
    op.execute("""
        LOCK TABLE langgraph_checkpoints.checkpoints, langgraph_checkpoints.checkpoint_blobs,
            langgraph_checkpoints.checkpoint_writes, langgraph_checkpoints.checkpoint_migrations
            IN ACCESS EXCLUSIVE MODE;
        DO $phase13_checkpoint_down$
        BEGIN
            IF EXISTS (SELECT 1 FROM langgraph_checkpoints.checkpoints)
               OR EXISTS (SELECT 1 FROM langgraph_checkpoints.checkpoint_blobs)
               OR EXISTS (SELECT 1 FROM langgraph_checkpoints.checkpoint_writes) THEN
                RAISE EXCEPTION 'Phase 13 checkpoint downgrade refused: recovery state exists; retain schema';
            END IF;
        END $phase13_checkpoint_down$;
        DROP TABLE langgraph_checkpoints.checkpoint_writes;
        DROP TABLE langgraph_checkpoints.checkpoint_blobs;
        DROP TABLE langgraph_checkpoints.checkpoints;
        DROP TABLE langgraph_checkpoints.checkpoint_migrations;
        DROP SCHEMA langgraph_checkpoints;
    """)
