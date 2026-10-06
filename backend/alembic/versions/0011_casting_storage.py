"""Independent engineering storage. Additive; old data is untouched.

Revision ID: 0011_casting_storage
Revises: 0010_phase13_checkpoints

Recovery: disable CASTING_DESIGN_ENABLED and retain the added tables/columns.
No automatic data-destructive downgrade is provided.
"""
from alembic import op

revision = "0011_casting_storage"
down_revision = "0010_phase13_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen DDL, no application imports or runtime metadata dependency.
    op.execute(r"""
CREATE TABLE casting_design_files (
	id UUID NOT NULL,
	session_id UUID NOT NULL,
	upload_request_id UUID,
	run_id UUID,
	execution_no INTEGER,
	artifact_name VARCHAR(100),
	kind VARCHAR(32) NOT NULL,
	original_filename VARCHAR(255) NOT NULL,
	content_type VARCHAR(100) NOT NULL,
	size_bytes BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	bucket VARCHAR(255) NOT NULL,
	object_key VARCHAR(512) NOT NULL,
	storage_state VARCHAR(16) DEFAULT 'pending' NOT NULL,
	admission_passed BOOLEAN DEFAULT 'false' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_casting_files_session_id UNIQUE (session_id, id),
	CONSTRAINT uq_casting_files_upload UNIQUE (session_id, upload_request_id),
	CONSTRAINT uq_casting_files_artifact UNIQUE (run_id, execution_no, artifact_name),
	CONSTRAINT uq_casting_files_object UNIQUE (bucket, object_key),
	CONSTRAINT ck_casting_files_size CHECK (size_bytes >= 0 AND size_bytes <= 33554432),
	CONSTRAINT ck_casting_files_state CHECK (storage_state IN ('pending', 'ready')),
	CONSTRAINT ck_casting_files_kind CHECK ((kind = 'input' AND upload_request_id IS NOT NULL AND run_id IS NULL AND execution_no IS NULL AND artifact_name IS NULL) OR (kind IN ('rules', 'engine', 'audit', 'recommendation') AND upload_request_id IS NULL AND run_id IS NOT NULL AND execution_no IS NOT NULL AND execution_no > 0 AND artifact_name IS NOT NULL)),
	FOREIGN KEY(session_id) REFERENCES qa_sessions (id)
);

CREATE INDEX ix_casting_design_files_session_id ON casting_design_files (session_id);

CREATE TABLE casting_design_runs (
	id UUID NOT NULL,
	session_id UUID NOT NULL,
	turn_id UUID NOT NULL,
	call_key VARCHAR(64) NOT NULL,
	input_file_id UUID NOT NULL,
	input_sha256 VARCHAR(64) NOT NULL,
	normalized_input_sha256 VARCHAR(64),
	rule_id VARCHAR(128) NOT NULL,
	rule_version VARCHAR(128) NOT NULL,
	rule_sha256 VARCHAR(64) NOT NULL,
	registry_sha256 VARCHAR(64) NOT NULL,
	project_key VARCHAR(128) NOT NULL,
	engine_id VARCHAR(128) NOT NULL,
	engine_version VARCHAR(128) NOT NULL,
	engine_sha256 VARCHAR(64) NOT NULL,
	engine_manifest_sha256 VARCHAR(64) NOT NULL,
	dependency_manifest_sha256 VARCHAR(64) NOT NULL,
	status VARCHAR(32) DEFAULT 'pending' NOT NULL,
	execution_no INTEGER DEFAULT '1' NOT NULL,
	result_file_id UUID,
	result_sha256 VARCHAR(64),
	recommended_candidate_id VARCHAR(128),
	candidate_count INTEGER,
	error JSONB,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_casting_runs_session_id UNIQUE (session_id, id),
	CONSTRAINT uq_casting_runs_turn UNIQUE (turn_id),
	CONSTRAINT uq_casting_runs_call UNIQUE (session_id, call_key),
	CONSTRAINT fk_casting_runs_turn FOREIGN KEY(session_id, turn_id) REFERENCES qa_turns (session_id, id),
	CONSTRAINT fk_casting_runs_input FOREIGN KEY(session_id, input_file_id) REFERENCES casting_design_files (session_id, id),
	CONSTRAINT ck_casting_runs_execution CHECK (execution_no > 0),
	CONSTRAINT ck_casting_runs_status CHECK (status IN ('pending', 'running', 'persisting', 'succeeded', 'no_feasible_candidate', 'admission_failed', 'engine_failed', 'timed_out', 'interrupted')),
	CONSTRAINT ck_casting_runs_result CHECK ((status IN ('succeeded', 'no_feasible_candidate') AND result_file_id IS NOT NULL AND result_sha256 IS NOT NULL AND finished_at IS NOT NULL AND candidate_count IS NOT NULL) OR (status NOT IN ('succeeded', 'no_feasible_candidate') AND result_file_id IS NULL AND result_sha256 IS NULL)),
	CONSTRAINT ck_casting_runs_count CHECK (candidate_count IS NULL OR candidate_count >= 0)
);

CREATE INDEX ix_casting_design_runs_session_id ON casting_design_runs (session_id);

ALTER TABLE qa_turns ADD COLUMN graph_version VARCHAR(64) DEFAULT NULL, ADD COLUMN requested_casting_input_file_id UUID DEFAULT NULL, ADD COLUMN effective_casting_input_file_id UUID DEFAULT NULL;

ALTER TABLE casting_design_files ADD CONSTRAINT fk_casting_files_run FOREIGN KEY(session_id, run_id) REFERENCES casting_design_runs (session_id, id);

ALTER TABLE casting_design_runs ADD CONSTRAINT fk_casting_runs_result FOREIGN KEY(session_id, result_file_id) REFERENCES casting_design_files (session_id, id);

ALTER TABLE qa_turns ADD CONSTRAINT fk_qa_turns_effective_casting FOREIGN KEY(session_id, effective_casting_input_file_id) REFERENCES casting_design_files (session_id, id);

ALTER TABLE qa_turns ADD CONSTRAINT fk_qa_turns_requested_casting FOREIGN KEY(session_id, requested_casting_input_file_id) REFERENCES casting_design_files (session_id, id);
    """)


def downgrade() -> None:
    raise RuntimeError("0011 retains engineering audit records; disable the feature and retain schema. Manual recovery requires an approved backup plan.")
