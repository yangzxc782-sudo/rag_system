"""Phase 13 M1 business conversation persistence.

Revision ID: 0009_phase13_chat_expand
Revises: 0008_phase10_enforce
Schema DDL is frozen here; this migration does not import application models.
Downgrade refuses to discard used Phase 13 data.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_phase13_chat_expand"
down_revision: str | None = "0008_phase10_enforce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREFLIGHT_SQL = """
LOCK TABLE qa_sessions, qa_messages, retrieval_logs IN SHARE ROW EXCLUSIVE MODE;
DO $phase13$
DECLARE bad_ids text;
BEGIN
    SELECT string_agg(id::text, ', ') INTO bad_ids FROM (
        SELECT m.id FROM qa_messages m LEFT JOIN qa_sessions s ON s.id=m.session_id
        WHERE s.id IS NULL ORDER BY m.id LIMIT 10
    ) bad;
    IF bad_ids IS NOT NULL THEN
        RAISE EXCEPTION 'Phase 13 preflight: orphan legacy messages: %', bad_ids;
    END IF;
    SELECT string_agg(id::text, ', ') INTO bad_ids FROM (
        SELECT l.id FROM retrieval_logs l
        LEFT JOIN qa_sessions s ON s.id=l.session_id
        LEFT JOIN qa_messages m ON m.id=l.message_id
        WHERE s.id IS NULL OR (l.message_id IS NOT NULL AND
            (m.id IS NULL OR m.session_id <> l.session_id))
        ORDER BY l.id LIMIT 10
    ) bad;
    IF bad_ids IS NOT NULL THEN
        RAISE EXCEPTION 'Phase 13 preflight: orphan or cross-session retrieval logs: %', bad_ids;
    END IF;
    SELECT string_agg(id::text, ', ') INTO bad_ids FROM (
        SELECT id FROM retrieval_logs
        WHERE result_summary IS NOT NULL
            AND result_summary NOT IN ('{}'::jsonb, 'null'::jsonb)
        ORDER BY id LIMIT 10
    ) bad;
    IF bad_ids IS NOT NULL THEN
        RAISE EXCEPTION 'Phase 13 preflight: legacy retrieval summaries lack normalized provenance; review ids: %', bad_ids;
    END IF;
END $phase13$;
"""

NEW_TABLES_SQL = """
CREATE TABLE qa_turns (
	id UUID NOT NULL,
	session_id UUID NOT NULL,
	request_id UUID NOT NULL,
	request_fingerprint VARCHAR(64) NOT NULL,
	turn_no BIGINT NOT NULL,
	question TEXT NOT NULL,
	retrieval_limit INTEGER NOT NULL,
	document_id UUID,
	status VARCHAR(32) DEFAULT 'running' NOT NULL,
	attempt_no INTEGER DEFAULT '1' NOT NULL,
	outcome VARCHAR(32),
	error_code VARCHAR(100),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_qa_turns_session_id UNIQUE (session_id, id),
	CONSTRAINT uq_qa_turns_request UNIQUE (session_id, request_id),
	CONSTRAINT uq_qa_turns_number UNIQUE (session_id, turn_no),
	CONSTRAINT ck_qa_turns_numbers CHECK (turn_no > 0 AND attempt_no > 0),
	CONSTRAINT ck_qa_turns_limit CHECK (retrieval_limit BETWEEN 1 AND 50),
	CONSTRAINT ck_qa_turns_status CHECK (status IN ('running', 'finalizing', 'completed', 'failed', 'needs_recovery')),
	CONSTRAINT ck_qa_turns_outcome CHECK (outcome IS NULL OR outcome IN ('answer', 'no_context', 'clarification')),
	CONSTRAINT ck_qa_turns_completion CHECK ((status = 'completed' AND completed_at IS NOT NULL AND outcome IS NOT NULL AND error_code IS NULL) OR (status <> 'completed' AND completed_at IS NULL AND outcome IS NULL)),
	CONSTRAINT ck_qa_turns_error CHECK ((status IN ('failed', 'needs_recovery') AND error_code IS NOT NULL) OR (status NOT IN ('failed', 'needs_recovery') AND error_code IS NULL)),
	FOREIGN KEY(session_id) REFERENCES qa_sessions (id)
);

CREATE UNIQUE INDEX uq_qa_turns_unresolved_session ON qa_turns (session_id) WHERE status IN ('running', 'finalizing', 'needs_recovery');

CREATE TABLE qa_turn_artifacts (
	id UUID NOT NULL,
	session_id UUID NOT NULL,
	turn_id UUID NOT NULL,
	attempt_no INTEGER NOT NULL,
	artifact_key VARCHAR(100) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	input_fingerprint VARCHAR(64) NOT NULL,
	content_fingerprint VARCHAR(64) NOT NULL,
	parent_artifact_id UUID,
	details JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_qa_artifacts_turn_identity UNIQUE (session_id, turn_id, id),
	CONSTRAINT uq_qa_artifacts_key UNIQUE (turn_id, attempt_no, artifact_key),
	CONSTRAINT fk_qa_artifacts_turn_session FOREIGN KEY(session_id, turn_id) REFERENCES qa_turns (session_id, id),
	CONSTRAINT fk_qa_artifacts_parent_turn FOREIGN KEY(session_id, turn_id, parent_artifact_id) REFERENCES qa_turn_artifacts (session_id, turn_id, id),
	CONSTRAINT ck_qa_artifacts_versions CHECK (attempt_no > 0 AND schema_version > 0),
	CONSTRAINT ck_qa_artifacts_kind CHECK (kind IN ('context', 'rewrite', 'retrieval', 'evidence', 'generation', 'result')),
	CONSTRAINT ck_qa_artifacts_parent CHECK (parent_artifact_id IS NULL OR parent_artifact_id <> id)
);

CREATE TABLE qa_evidence_snapshots (
	id UUID NOT NULL,
	session_id UUID NOT NULL,
	turn_id UUID NOT NULL,
	artifact_id UUID NOT NULL,
	snapshot_key VARCHAR(100) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	payload JSONB,
	content_fingerprint VARCHAR(64) NOT NULL,
	status VARCHAR(32) DEFAULT 'available' NOT NULL,
	redacted_document_id UUID,
	redacted_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_qa_snapshots_turn_identity UNIQUE (session_id, turn_id, id),
	CONSTRAINT uq_qa_snapshots_key UNIQUE (artifact_id, snapshot_key),
	CONSTRAINT fk_qa_snapshots_artifact_turn FOREIGN KEY(session_id, turn_id, artifact_id) REFERENCES qa_turn_artifacts (session_id, turn_id, id),
	CONSTRAINT ck_qa_snapshots_version CHECK (schema_version > 0),
	CONSTRAINT ck_qa_snapshots_kind CHECK (kind IN ('candidate', 'citation', 'graph', 'answer_draft')),
	CONSTRAINT ck_qa_snapshots_redaction CHECK ((status = 'available' AND payload IS NOT NULL AND redacted_at IS NULL AND redacted_document_id IS NULL) OR (status = 'source_deleted' AND payload IS NULL AND redacted_at IS NOT NULL AND redacted_document_id IS NOT NULL))
);

CREATE TABLE qa_evidence_sources (
	id UUID NOT NULL,
	session_id UUID NOT NULL,
	turn_id UUID NOT NULL,
	snapshot_id UUID NOT NULL,
	document_id UUID NOT NULL,
	chunk_id UUID,
	status VARCHAR(32) DEFAULT 'available' NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_qa_sources_snapshot_turn FOREIGN KEY(session_id, turn_id, snapshot_id) REFERENCES qa_evidence_snapshots (session_id, turn_id, id),
	CONSTRAINT ck_qa_sources_deletion CHECK ((status = 'available' AND deleted_at IS NULL) OR (status = 'source_deleted' AND deleted_at IS NOT NULL))
);

CREATE INDEX ix_qa_sources_document ON qa_evidence_sources (document_id, snapshot_id);

CREATE INDEX ix_qa_sources_snapshot ON qa_evidence_sources (snapshot_id);

CREATE UNIQUE INDEX uq_qa_sources_chunk ON qa_evidence_sources (snapshot_id, chunk_id) WHERE chunk_id IS NOT NULL;

CREATE UNIQUE INDEX uq_qa_sources_document_only ON qa_evidence_sources (snapshot_id, document_id) WHERE chunk_id IS NULL;
"""

EXISTING_CONSTRAINTS_SQL = """
ALTER TABLE qa_messages ADD CONSTRAINT fk_qa_messages_answer_turn
    FOREIGN KEY(session_id, turn_id, answer_snapshot_id)
    REFERENCES qa_evidence_snapshots(session_id, turn_id, id);
ALTER TABLE qa_messages ADD CONSTRAINT ck_qa_messages_answer_reference CHECK (
    (turn_id IS NULL AND answer_snapshot_id IS NULL) OR
    (turn_id IS NOT NULL AND ((role = 'user' AND answer_snapshot_id IS NULL) OR
    (role = 'assistant' AND answer_snapshot_id IS NOT NULL))));
ALTER TABLE qa_sessions ADD CONSTRAINT ck_qa_sessions_counters CHECK (next_turn_no > 0 AND next_message_seq > 0);
ALTER TABLE qa_sessions ADD CONSTRAINT ck_qa_sessions_creation_identity CHECK ((create_request_id IS NULL AND create_fingerprint IS NULL) OR (create_request_id IS NOT NULL AND create_fingerprint IS NOT NULL));
ALTER TABLE qa_sessions ADD CONSTRAINT uq_qa_sessions_create_request UNIQUE (create_request_id);
ALTER TABLE qa_messages ADD CONSTRAINT ck_qa_messages_sequence CHECK (sequence_no > 0);
ALTER TABLE qa_messages ADD CONSTRAINT ck_qa_messages_turn_role CHECK (turn_id IS NULL OR role IN ('user', 'assistant'));
ALTER TABLE qa_messages ADD CONSTRAINT fk_qa_messages_turn_session FOREIGN KEY(session_id, turn_id) REFERENCES qa_turns (session_id, id);
ALTER TABLE qa_messages ADD CONSTRAINT uq_qa_messages_sequence UNIQUE (session_id, sequence_no);
ALTER TABLE qa_messages ADD CONSTRAINT uq_qa_messages_session_id UNIQUE (session_id, id);
ALTER TABLE qa_messages ADD CONSTRAINT uq_qa_messages_turn_identity UNIQUE (session_id, turn_id, id);
ALTER TABLE qa_messages ADD CONSTRAINT uq_qa_messages_turn_role UNIQUE (turn_id, role);
ALTER TABLE retrieval_logs ADD CONSTRAINT ck_retrieval_logs_turn_generation CHECK ((turn_id IS NULL AND evidence_generation IS NULL) OR (turn_id IS NOT NULL AND evidence_generation > 0 AND evidence_generation IS NOT NULL AND message_id IS NOT NULL));
ALTER TABLE retrieval_logs ADD CONSTRAINT fk_retrieval_logs_message_session FOREIGN KEY(session_id, message_id) REFERENCES qa_messages (session_id, id);
ALTER TABLE retrieval_logs ADD CONSTRAINT fk_retrieval_logs_message_turn FOREIGN KEY(session_id, turn_id, message_id) REFERENCES qa_messages (session_id, turn_id, id);
ALTER TABLE retrieval_logs ADD CONSTRAINT fk_retrieval_logs_turn_session FOREIGN KEY(session_id, turn_id) REFERENCES qa_turns (session_id, id);
ALTER TABLE retrieval_logs ADD CONSTRAINT uq_retrieval_logs_turn_generation UNIQUE (turn_id, evidence_generation);
"""

DOWNGRADE_PREFLIGHT_SQL = """
LOCK TABLE qa_sessions, qa_messages, retrieval_logs, qa_turns, qa_turn_artifacts,
    qa_evidence_snapshots, qa_evidence_sources IN ACCESS EXCLUSIVE MODE;
DO $phase13_down$
BEGIN
    IF EXISTS (SELECT 1 FROM qa_turns)
       OR EXISTS (SELECT 1 FROM qa_turn_artifacts)
       OR EXISTS (SELECT 1 FROM qa_evidence_snapshots)
       OR EXISTS (SELECT 1 FROM qa_evidence_sources)
       OR EXISTS (SELECT 1 FROM qa_sessions WHERE create_request_id IS NOT NULL)
       OR EXISTS (SELECT 1 FROM qa_messages WHERE turn_id IS NOT NULL)
       OR EXISTS (SELECT 1 FROM retrieval_logs WHERE turn_id IS NOT NULL) THEN
        RAISE EXCEPTION 'Phase 13 downgrade refused: conversation data is in use; retain schema and roll back application only';
    END IF;
END $phase13_down$;
"""


def upgrade() -> None:
    op.execute(PREFLIGHT_SQL)
    op.add_column("qa_sessions", sa.Column("create_request_id", postgresql.UUID(as_uuid=True)))
    op.add_column("qa_sessions", sa.Column("create_fingerprint", sa.String(64)))
    op.add_column("qa_sessions", sa.Column("next_turn_no", sa.BigInteger(), server_default="1", nullable=False))
    op.add_column("qa_sessions", sa.Column("next_message_seq", sa.BigInteger(), server_default="1", nullable=False))
    op.add_column("qa_messages", sa.Column("turn_id", postgresql.UUID(as_uuid=True)))
    op.add_column("qa_messages", sa.Column("answer_snapshot_id", postgresql.UUID(as_uuid=True)))
    op.add_column("qa_messages", sa.Column("sequence_no", sa.BigInteger(), nullable=True))
    op.add_column("retrieval_logs", sa.Column("turn_id", postgresql.UUID(as_uuid=True)))
    op.add_column("retrieval_logs", sa.Column("evidence_generation", sa.Integer()))
    op.execute("""
        WITH numbered AS (
            SELECT id, row_number() OVER (
                PARTITION BY session_id ORDER BY created_at, id
            ) AS seq FROM qa_messages
        )
        UPDATE qa_messages m SET sequence_no=n.seq
        FROM numbered n WHERE m.id=n.id
    """)
    op.execute("""
        UPDATE qa_sessions s SET next_message_seq=(
            SELECT COALESCE(MAX(m.sequence_no), 0) + 1
            FROM qa_messages m WHERE m.session_id=s.id
        ) WHERE EXISTS (SELECT 1 FROM qa_messages m WHERE m.session_id=s.id)
    """)
    op.alter_column("qa_messages", "sequence_no", existing_type=sa.BigInteger(), nullable=False)
    op.execute(NEW_TABLES_SQL)
    op.execute(EXISTING_CONSTRAINTS_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_PREFLIGHT_SQL)
    op.drop_constraint("fk_qa_messages_answer_turn", "qa_messages")
    op.drop_constraint("ck_qa_messages_answer_reference", "qa_messages")
    op.drop_constraint("uq_retrieval_logs_turn_generation", "retrieval_logs")
    op.drop_constraint("fk_retrieval_logs_turn_session", "retrieval_logs")
    op.drop_constraint("fk_retrieval_logs_message_turn", "retrieval_logs")
    op.drop_constraint("fk_retrieval_logs_message_session", "retrieval_logs")
    op.drop_constraint("ck_retrieval_logs_turn_generation", "retrieval_logs")
    op.drop_constraint("uq_qa_messages_turn_role", "qa_messages")
    op.drop_constraint("uq_qa_messages_turn_identity", "qa_messages")
    op.drop_constraint("uq_qa_messages_session_id", "qa_messages")
    op.drop_constraint("uq_qa_messages_sequence", "qa_messages")
    op.drop_constraint("fk_qa_messages_turn_session", "qa_messages")
    op.drop_constraint("ck_qa_messages_turn_role", "qa_messages")
    op.drop_constraint("ck_qa_messages_sequence", "qa_messages")
    op.drop_constraint("uq_qa_sessions_create_request", "qa_sessions")
    op.drop_constraint("ck_qa_sessions_creation_identity", "qa_sessions")
    op.drop_constraint("ck_qa_sessions_counters", "qa_sessions")
    op.drop_table("qa_evidence_sources")
    op.drop_table("qa_evidence_snapshots")
    op.drop_table("qa_turn_artifacts")
    op.drop_table("qa_turns")
    op.drop_column("retrieval_logs", "evidence_generation")
    op.drop_column("retrieval_logs", "turn_id")
    op.drop_column("qa_messages", "sequence_no")
    op.drop_column("qa_messages", "answer_snapshot_id")
    op.drop_column("qa_messages", "turn_id")
    op.drop_column("qa_sessions", "next_message_seq")
    op.drop_column("qa_sessions", "next_turn_no")
    op.drop_column("qa_sessions", "create_fingerprint")
    op.drop_column("qa_sessions", "create_request_id")
