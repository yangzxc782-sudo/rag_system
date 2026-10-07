"""PDF/KG M0 persistence, additive and legacy preserving.

Revision ID: 0013_pdf_kg_versions
Revises: 0012_casting_answers

Only DDL; no runtime application imports, data backfill or external IO.
Recovery: keep schema/data and roll back application deployment. Executing this
migration requires separate authorization; destructive downgrade is refused.
"""
from alembic import op

revision = "0013_pdf_kg_versions"
down_revision = "0012_casting_answers"
branch_labels = None
depends_on = None

# Frozen PostgreSQL DDL; never derive migrations from live application models.
TABLE_DDL = r"""
ALTER TABLE document_parse_runs ADD CONSTRAINT uq_document_parse_runs_owner UNIQUE (id, document_id);

CREATE TABLE document_source_versions (
	source_version UUID NOT NULL, 
	document_id UUID NOT NULL, 
	parse_run_id UUID NOT NULL, 
	bucket_name VARCHAR(255) NOT NULL, 
	canonical_object_key VARCHAR(1024) NOT NULL, 
	canonical_sha256 VARCHAR(64) NOT NULL, 
	character_count BIGINT NOT NULL, 
	block_map_object_key VARCHAR(1024) NOT NULL, 
	block_map_sha256 VARCHAR(64) NOT NULL, 
	cleaner_version VARCHAR(100) NOT NULL, 
	renderer_version VARCHAR(100) NOT NULL, 
	cleaning_config_sha256 VARCHAR(64) NOT NULL, 
	coordinate_unit VARCHAR(32) DEFAULT 'unicode_code_point' NOT NULL, 
	normalization VARCHAR(32) DEFAULT 'LF_NFC_UTF8_v1' NOT NULL, 
	frozen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (source_version), 
	CONSTRAINT uq_source_versions_document UNIQUE (source_version, document_id), 
	CONSTRAINT uq_source_versions_parse UNIQUE (source_version, document_id, parse_run_id), 
	CONSTRAINT fk_source_versions_parse_owner FOREIGN KEY(parse_run_id, document_id) REFERENCES document_parse_runs (id, document_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_source_versions_length CHECK (character_count > 0), 
	CONSTRAINT ck_source_versions_hashes CHECK (canonical_sha256 ~ '^[a-f0-9]{64}$' AND block_map_sha256 ~ '^[a-f0-9]{64}$' AND cleaning_config_sha256 ~ '^[a-f0-9]{64}$'), 
	CONSTRAINT ck_source_versions_fields CHECK (length(btrim(bucket_name)) > 0 AND length(btrim(canonical_object_key)) > 0 AND length(btrim(block_map_object_key)) > 0 AND length(btrim(cleaner_version)) > 0 AND length(btrim(renderer_version)) > 0), 
	CONSTRAINT ck_source_versions_coordinates CHECK (coordinate_unit = 'unicode_code_point' AND normalization = 'LF_NFC_UTF8_v1'), 
	CONSTRAINT uq_source_versions_object UNIQUE (bucket_name, canonical_object_key)
);

CREATE INDEX ix_source_versions_document ON document_source_versions (document_id);

CREATE INDEX ix_source_versions_parse ON document_source_versions (parse_run_id);

CREATE TABLE document_graph_builds (
	id UUID NOT NULL, 
	document_id UUID NOT NULL, 
	source_version UUID NOT NULL, 
	graph_id VARCHAR(128) NOT NULL, 
	identity_day DATE NOT NULL, 
	source_path VARCHAR(1024) NOT NULL, 
	graph_schema_version INTEGER DEFAULT '2' NOT NULL, 
	template_version VARCHAR(100) NOT NULL, 
	template_sha256 VARCHAR(64) NOT NULL, 
	unit_rule_version VARCHAR(100) NOT NULL, 
	unit_rule_sha256 VARCHAR(64) NOT NULL, 
	provider_fingerprint VARCHAR(64) NOT NULL, 
	input_fingerprint VARCHAR(64) NOT NULL, 
	status VARCHAR(32) DEFAULT 'pending' NOT NULL, 
	result_object_key VARCHAR(1024), 
	result_sha256 VARCHAR(64), 
	unit_count INTEGER, 
	anchor_count INTEGER, 
	entity_count INTEGER, 
	relationship_count INTEGER, 
	write_checkpoint JSONB DEFAULT '{}'::jsonb NOT NULL, 
	last_error_code VARCHAR(100), 
	sealed_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_graph_builds_source_owner UNIQUE (id, document_id, source_version), 
	CONSTRAINT uq_graph_builds_graph_id UNIQUE (graph_id), 
	CONSTRAINT uq_graph_builds_source_path UNIQUE (source_path), 
	CONSTRAINT fk_graph_builds_source_owner FOREIGN KEY(source_version, document_id) REFERENCES document_source_versions (source_version, document_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_graph_builds_status CHECK (status IN ('pending', 'extracting', 'extraction_failed', 'partial_failed', 'writing', 'write_failed', 'ready', 'ready_empty', 'cancelled')), 
	CONSTRAINT ck_graph_builds_schema CHECK (graph_schema_version = 2), 
	CONSTRAINT ck_graph_builds_fields CHECK (length(btrim(graph_id)) > 0 AND length(btrim(source_path)) > 0 AND length(btrim(template_version)) > 0 AND length(btrim(unit_rule_version)) > 0), 
	CONSTRAINT ck_graph_builds_hashes CHECK (template_sha256 ~ '^[a-f0-9]{64}$' AND unit_rule_sha256 ~ '^[a-f0-9]{64}$' AND provider_fingerprint ~ '^[a-f0-9]{64}$' AND input_fingerprint ~ '^[a-f0-9]{64}$' AND (result_sha256 IS NULL OR result_sha256 ~ '^[a-f0-9]{64}$')), 
	CONSTRAINT ck_graph_builds_result_pair CHECK ((result_object_key IS NULL AND result_sha256 IS NULL) OR (result_object_key IS NOT NULL AND length(btrim(result_object_key)) > 0 AND result_sha256 IS NOT NULL)), 
	CONSTRAINT ck_graph_builds_counts CHECK (unit_count >= 0 AND anchor_count >= 0 AND entity_count >= 0 AND relationship_count >= 0 AND anchor_count <= unit_count), 
	CONSTRAINT ck_graph_builds_sealed CHECK ((status IN ('ready', 'ready_empty') AND sealed_at IS NOT NULL AND result_object_key IS NOT NULL AND unit_count IS NOT NULL AND anchor_count IS NOT NULL AND entity_count IS NOT NULL AND relationship_count IS NOT NULL) OR (status NOT IN ('ready', 'ready_empty') AND sealed_at IS NULL)), 
	CONSTRAINT ck_graph_builds_empty CHECK ((status <> 'ready' OR (anchor_count > 0 AND entity_count > 0 AND relationship_count > 0)) AND (status <> 'ready_empty' OR (anchor_count = 0 AND entity_count = 0 AND relationship_count = 0))), 
	CONSTRAINT ck_graph_builds_checkpoint CHECK (jsonb_typeof(write_checkpoint) = 'object' AND octet_length(write_checkpoint::text) <= 65536)
);

CREATE INDEX ix_graph_builds_document ON document_graph_builds (document_id);

CREATE INDEX ix_graph_builds_source ON document_graph_builds (source_version, status);

CREATE TABLE kg_extraction_units (
	id UUID NOT NULL, 
	document_id UUID NOT NULL, 
	graph_build_id UUID NOT NULL, 
	source_version UUID NOT NULL, 
	source_start BIGINT NOT NULL, 
	source_end BIGINT NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	unit_index INTEGER NOT NULL, 
	allocated_anchor_id VARCHAR(512) NOT NULL, 
	allocated_anchor_metadata JSONB NOT NULL, 
	status VARCHAR(32) DEFAULT 'pending' NOT NULL, 
	has_qualified_triples BOOLEAN DEFAULT 'false' NOT NULL, 
	input_sha256 VARCHAR(64) NOT NULL, 
	result_object_key VARCHAR(1024), 
	result_sha256 VARCHAR(64), 
	piece_checkpoints JSONB DEFAULT '{}'::jsonb NOT NULL, 
	last_error_code VARCHAR(100), 
	completed_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_kg_units_build_owner FOREIGN KEY(graph_build_id, document_id, source_version) REFERENCES document_graph_builds (id, document_id, source_version) ON DELETE RESTRICT, 
	CONSTRAINT uq_kg_units_order UNIQUE (graph_build_id, unit_index), 
	CONSTRAINT uq_kg_units_anchor UNIQUE (graph_build_id, allocated_anchor_id), 
	CONSTRAINT ck_kg_units_range CHECK (unit_index >= 0 AND source_start >= 0 AND source_end > source_start), 
	CONSTRAINT ck_kg_units_kind CHECK (kind IN ('table', 'clause')), 
	CONSTRAINT ck_kg_units_status CHECK (status IN ('pending', 'extracting', 'succeeded_nonempty', 'succeeded_empty', 'failed', 'partial_failed')), 
	CONSTRAINT ck_kg_units_qualified CHECK (has_qualified_triples = (status = 'succeeded_nonempty')), 
	CONSTRAINT ck_kg_units_anchor_metadata CHECK (length(btrim(allocated_anchor_id)) > 0 AND jsonb_typeof(allocated_anchor_metadata) = 'object' AND octet_length(allocated_anchor_metadata::text) <= 65536), 
	CONSTRAINT ck_kg_units_hashes CHECK (input_sha256 ~ '^[a-f0-9]{64}$' AND (result_sha256 IS NULL OR result_sha256 ~ '^[a-f0-9]{64}$')), 
	CONSTRAINT ck_kg_units_result_pair CHECK ((result_object_key IS NULL AND result_sha256 IS NULL) OR (result_object_key IS NOT NULL AND length(btrim(result_object_key)) > 0 AND result_sha256 IS NOT NULL)), 
	CONSTRAINT ck_kg_units_completed CHECK (status NOT IN ('succeeded_nonempty', 'succeeded_empty') OR (result_object_key IS NOT NULL AND completed_at IS NOT NULL)), 
	CONSTRAINT ck_kg_units_checkpoints CHECK (jsonb_typeof(piece_checkpoints) = 'object' AND octet_length(piece_checkpoints::text) <= 65536)
);

CREATE INDEX ix_kg_units_document ON kg_extraction_units (document_id);

CREATE INDEX ix_kg_units_interval ON kg_extraction_units (source_version, graph_build_id, source_start, source_end);

CREATE TABLE document_chunk_sets (
	id UUID NOT NULL, 
	document_id UUID NOT NULL, 
	source_version UUID NOT NULL, 
	graph_build_id UUID NOT NULL, 
	segmentation_version VARCHAR(100) NOT NULL, 
	segmentation_config JSONB NOT NULL, 
	segmentation_config_sha256 VARCHAR(64) NOT NULL, 
	embedding_fingerprint VARCHAR(64) NOT NULL, 
	status VARCHAR(32) DEFAULT 'pending' NOT NULL, 
	chunk_count INTEGER, 
	manifest_object_key VARCHAR(1024), 
	manifest_sha256 VARCHAR(64), 
	sealed_at TIMESTAMP WITH TIME ZONE, 
	index_name VARCHAR(255), 
	index_receipt JSONB, 
	indexed_at TIMESTAMP WITH TIME ZONE, 
	last_error_code VARCHAR(100), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chunk_sets_document UNIQUE (id, document_id), 
	CONSTRAINT uq_chunk_sets_source_owner UNIQUE (id, document_id, source_version), 
	CONSTRAINT uq_chunk_sets_build_owner UNIQUE (id, document_id, source_version, graph_build_id), 
	CONSTRAINT fk_chunk_sets_build_owner FOREIGN KEY(graph_build_id, document_id, source_version) REFERENCES document_graph_builds (id, document_id, source_version) ON DELETE RESTRICT, 
	CONSTRAINT ck_chunk_sets_status CHECK (status IN ('pending', 'chunking', 'chunks_ready', 'embedding', 'indexing', 'indexed', 'failed', 'cancelled')), 
	CONSTRAINT ck_chunk_sets_version CHECK (length(btrim(segmentation_version)) > 0), 
	CONSTRAINT ck_chunk_sets_config CHECK (jsonb_typeof(segmentation_config) = 'object' AND octet_length(segmentation_config::text) <= 16384), 
	CONSTRAINT ck_chunk_sets_hashes CHECK (segmentation_config_sha256 ~ '^[a-f0-9]{64}$' AND embedding_fingerprint ~ '^[a-f0-9]{64}$' AND (manifest_sha256 IS NULL OR manifest_sha256 ~ '^[a-f0-9]{64}$')), 
	CONSTRAINT ck_chunk_sets_sealed CHECK ((sealed_at IS NULL AND chunk_count IS NULL AND manifest_object_key IS NULL AND manifest_sha256 IS NULL) OR (sealed_at IS NOT NULL AND chunk_count IS NOT NULL AND chunk_count > 0 AND manifest_object_key IS NOT NULL AND length(btrim(manifest_object_key)) > 0 AND manifest_sha256 IS NOT NULL)), 
	CONSTRAINT ck_chunk_sets_ready CHECK (status NOT IN ('chunks_ready', 'embedding', 'indexing', 'indexed') OR sealed_at IS NOT NULL), 
	CONSTRAINT ck_chunk_sets_indexed CHECK ((status = 'indexed' AND indexed_at IS NOT NULL AND index_name IS NOT NULL AND index_receipt IS NOT NULL) OR (status <> 'indexed' AND indexed_at IS NULL)), 
	CONSTRAINT ck_chunk_sets_index_receipt CHECK ((index_name IS NULL OR length(btrim(index_name)) > 0) AND (index_receipt IS NULL OR (jsonb_typeof(index_receipt) = 'object' AND octet_length(index_receipt::text) <= 65536)))
);

CREATE INDEX ix_chunk_sets_build ON document_chunk_sets (graph_build_id);

CREATE INDEX ix_chunk_sets_document ON document_chunk_sets (document_id, status);

CREATE INDEX ix_chunk_sets_source ON document_chunk_sets (source_version);

CREATE TABLE document_processing_jobs (
	id UUID NOT NULL, 
	document_id UUID NOT NULL, 
	operation VARCHAR(16) NOT NULL, 
	request_id UUID NOT NULL, 
	input_fingerprint VARCHAR(64) NOT NULL, 
	source_version UUID, 
	graph_build_id UUID, 
	chunk_set_id UUID, 
	stage VARCHAR(32) NOT NULL, 
	status VARCHAR(16) DEFAULT 'queued' NOT NULL, 
	checkpoint JSONB DEFAULT '{}'::jsonb NOT NULL, 
	fencing_token BIGINT DEFAULT '0' NOT NULL, 
	attempt_count INTEGER DEFAULT '0' NOT NULL, 
	max_attempts INTEGER DEFAULT '3' NOT NULL, 
	locked_by VARCHAR(255), 
	lease_token UUID, 
	locked_at TIMESTAMP WITH TIME ZONE, 
	lease_expires_at TIMESTAMP WITH TIME ZONE, 
	next_retry_at TIMESTAMP WITH TIME ZONE, 
	last_error_code VARCHAR(100), 
	finished_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_processing_jobs_request UNIQUE (document_id, operation, request_id), 
	CONSTRAINT fk_processing_jobs_source_owner FOREIGN KEY(source_version, document_id) REFERENCES document_source_versions (source_version, document_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_processing_jobs_build_owner FOREIGN KEY(graph_build_id, document_id, source_version) REFERENCES document_graph_builds (id, document_id, source_version) ON DELETE RESTRICT, 
	CONSTRAINT fk_processing_jobs_set_owner FOREIGN KEY(chunk_set_id, document_id, source_version, graph_build_id) REFERENCES document_chunk_sets (id, document_id, source_version, graph_build_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_processing_jobs_operation CHECK (operation IN ('process', 'rechunk')), 
	CONSTRAINT ck_processing_jobs_status CHECK (status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled')), 
	CONSTRAINT ck_processing_jobs_stage CHECK (stage IN ('uploaded', 'parsing', 'parsed', 'cleaning', 'source_ready', 'kg_extracting', 'kg_writing', 'kg_ready', 'chunking', 'chunks_ready', 'embedding', 'indexing', 'indexed')), 
	CONSTRAINT ck_processing_jobs_versions CHECK ((graph_build_id IS NULL OR source_version IS NOT NULL) AND (chunk_set_id IS NULL OR (graph_build_id IS NOT NULL AND source_version IS NOT NULL)) AND (operation <> 'rechunk' OR (source_version IS NOT NULL AND graph_build_id IS NOT NULL AND stage IN ('kg_ready', 'chunking', 'chunks_ready', 'embedding', 'indexing', 'indexed')))), 
	CONSTRAINT ck_processing_jobs_input CHECK (input_fingerprint ~ '^[a-f0-9]{64}$'), 
	CONSTRAINT ck_processing_jobs_checkpoint CHECK (jsonb_typeof(checkpoint) = 'object' AND octet_length(checkpoint::text) <= 65536), 
	CONSTRAINT ck_processing_jobs_attempts CHECK (fencing_token >= 0 AND attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts), 
	CONSTRAINT ck_processing_jobs_lease CHECK ((status = 'running' AND locked_by IS NOT NULL AND length(btrim(locked_by)) > 0 AND lease_token IS NOT NULL AND locked_at IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_expires_at > locked_at AND fencing_token > 0 AND attempt_count > 0) OR (status <> 'running' AND locked_by IS NULL AND lease_token IS NULL AND locked_at IS NULL AND lease_expires_at IS NULL)), 
	CONSTRAINT ck_processing_jobs_retry CHECK ((status = 'retry_wait' AND next_retry_at IS NOT NULL) OR (status <> 'retry_wait' AND next_retry_at IS NULL)), 
	CONSTRAINT ck_processing_jobs_finished CHECK ((status IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NOT NULL) OR (status NOT IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NULL)), 
	CONSTRAINT ck_processing_jobs_success CHECK (status <> 'succeeded' OR (stage = 'indexed' AND chunk_set_id IS NOT NULL)), 
	CONSTRAINT ck_processing_jobs_error CHECK (status NOT IN ('failed', 'retry_wait') OR (last_error_code IS NOT NULL AND length(btrim(last_error_code)) > 0)), 
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE RESTRICT
);

CREATE INDEX ix_processing_jobs_build ON document_processing_jobs (graph_build_id);

CREATE INDEX ix_processing_jobs_claimable ON document_processing_jobs (status, next_retry_at, lease_expires_at, created_at);

CREATE INDEX ix_processing_jobs_set ON document_processing_jobs (chunk_set_id);

CREATE INDEX ix_processing_jobs_source ON document_processing_jobs (source_version);

CREATE UNIQUE INDEX uq_processing_jobs_active_document ON document_processing_jobs (document_id) WHERE status IN ('queued', 'running', 'retry_wait');

ALTER TABLE documents ADD COLUMN current_chunk_set_id UUID DEFAULT NULL;

ALTER TABLE documents ADD COLUMN publication_revision BIGINT DEFAULT '0' NOT NULL;

ALTER TABLE documents ADD CONSTRAINT fk_documents_current_chunk_set FOREIGN KEY(current_chunk_set_id, id) REFERENCES document_chunk_sets (id, document_id) ON DELETE RESTRICT;

ALTER TABLE documents ADD CONSTRAINT ck_documents_publication_revision CHECK (publication_revision >= 0 AND (current_chunk_set_id IS NULL OR publication_revision > 0));

CREATE INDEX ix_documents_current_chunk_set ON documents (current_chunk_set_id);

ALTER TABLE document_chunks ADD COLUMN chunk_set_id UUID DEFAULT NULL;

ALTER TABLE document_chunks ADD COLUMN source_version UUID DEFAULT NULL;

ALTER TABLE document_chunks ADD COLUMN source_start BIGINT DEFAULT NULL;

ALTER TABLE document_chunks ADD COLUMN source_end BIGINT DEFAULT NULL;

ALTER TABLE document_chunks ADD COLUMN content_sha256 VARCHAR(64) DEFAULT NULL;

ALTER TABLE document_chunks ADD CONSTRAINT uq_document_chunks_set_index UNIQUE (chunk_set_id, chunk_index);

ALTER TABLE document_chunks ADD CONSTRAINT fk_document_chunks_set_owner FOREIGN KEY(chunk_set_id, document_id, source_version) REFERENCES document_chunk_sets (id, document_id, source_version) ON DELETE RESTRICT;

ALTER TABLE document_chunks ADD CONSTRAINT fk_document_chunks_source_parse FOREIGN KEY(source_version, document_id, parse_run_id) REFERENCES document_source_versions (source_version, document_id, parse_run_id) ON DELETE RESTRICT;

ALTER TABLE document_chunks ADD CONSTRAINT ck_document_chunks_source_contract CHECK ((chunk_set_id IS NULL AND source_version IS NULL AND source_start IS NULL AND source_end IS NULL AND content_sha256 IS NULL) OR (chunk_set_id IS NOT NULL AND source_version IS NOT NULL AND source_start IS NOT NULL AND source_end IS NOT NULL AND content_sha256 IS NOT NULL AND parse_run_id IS NOT NULL AND source_start >= 0 AND source_end > source_start AND chunk_index >= 0 AND char_length(content) = source_end - source_start AND content_sha256 ~ '^[a-f0-9]{64}$'));

CREATE INDEX ix_document_chunks_source_interval ON document_chunks (source_version, source_start, source_end);
"""

# Cross-row bounds and immutability cannot be expressed as ordinary CHECKs.
# These guards do not implement processing, external validation or task claims.
GUARD_DDL = r"""
CREATE FUNCTION pdf_kg_m0_guard_source() RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF NOT EXISTS (SELECT 1 FROM documents WHERE id = OLD.document_id AND deletion_status = 'deleting') THEN
            RAISE EXCEPTION 'PDF_KG_SOURCE_DELETE_REQUIRES_DOCUMENT_DELETION' USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'PDF_KG_SOURCE_IMMUTABLE' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_pdf_kg_source_immutable BEFORE UPDATE OR DELETE ON document_source_versions
FOR EACH ROW EXECUTE FUNCTION pdf_kg_m0_guard_source();

CREATE FUNCTION pdf_kg_m0_guard_build() RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.sealed_at IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM documents WHERE id = OLD.document_id AND deletion_status = 'deleting'
        ) THEN
            RAISE EXCEPTION 'PDF_KG_SEALED_BUILD_DELETE' USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    IF ROW(NEW.id, NEW.document_id, NEW.source_version, NEW.graph_id, NEW.identity_day, NEW.source_path,
           NEW.graph_schema_version, NEW.template_version, NEW.template_sha256, NEW.unit_rule_version,
           NEW.unit_rule_sha256, NEW.provider_fingerprint, NEW.input_fingerprint, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.document_id, OLD.source_version, OLD.graph_id, OLD.identity_day, OLD.source_path,
           OLD.graph_schema_version, OLD.template_version, OLD.template_sha256, OLD.unit_rule_version,
           OLD.unit_rule_sha256, OLD.provider_fingerprint, OLD.input_fingerprint, OLD.created_at)
       OR (OLD.sealed_at IS NOT NULL AND NEW IS DISTINCT FROM OLD) THEN
        RAISE EXCEPTION 'PDF_KG_BUILD_IDENTITY_OR_RESULT_IMMUTABLE' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_pdf_kg_build_immutable BEFORE UPDATE OR DELETE ON document_graph_builds
FOR EACH ROW EXECUTE FUNCTION pdf_kg_m0_guard_build();

CREATE FUNCTION pdf_kg_m0_guard_unit() RETURNS trigger LANGUAGE plpgsql AS $guard$
DECLARE
    parent_id uuid;
    parent_sealed timestamptz;
    source_length bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN parent_id := OLD.graph_build_id; ELSE parent_id := NEW.graph_build_id; END IF;
    -- Serialize unit writes with parent sealing; callers retain Document-first lock order.
    SELECT sealed_at INTO parent_sealed FROM document_graph_builds WHERE id = parent_id FOR UPDATE;
    IF parent_sealed IS NOT NULL THEN
        IF TG_OP = 'DELETE' AND EXISTS (
            SELECT 1 FROM documents WHERE id = OLD.document_id AND deletion_status = 'deleting'
        ) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'PDF_KG_SEALED_BUILD_UNITS_IMMUTABLE' USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('succeeded_nonempty', 'succeeded_empty') AND NOT EXISTS (
            SELECT 1 FROM documents WHERE id = OLD.document_id AND deletion_status = 'deleting'
        ) THEN
            RAISE EXCEPTION 'PDF_KG_SUCCESSFUL_UNIT_DELETE' USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.id, NEW.document_id, NEW.graph_build_id, NEW.source_version, NEW.source_start, NEW.source_end,
               NEW.kind, NEW.unit_index, NEW.allocated_anchor_id, NEW.allocated_anchor_metadata, NEW.input_sha256, NEW.created_at)
           IS DISTINCT FROM
           ROW(OLD.id, OLD.document_id, OLD.graph_build_id, OLD.source_version, OLD.source_start, OLD.source_end,
               OLD.kind, OLD.unit_index, OLD.allocated_anchor_id, OLD.allocated_anchor_metadata, OLD.input_sha256, OLD.created_at)
           OR (OLD.status IN ('succeeded_nonempty', 'succeeded_empty') AND NEW IS DISTINCT FROM OLD) THEN
            RAISE EXCEPTION 'PDF_KG_UNIT_IDENTITY_OR_RESULT_IMMUTABLE' USING ERRCODE = '23514';
        END IF;
    END IF;
    SELECT character_count INTO source_length FROM document_source_versions WHERE source_version = NEW.source_version;
    IF NEW.source_end > source_length THEN
        RAISE EXCEPTION 'PDF_KG_UNIT_RANGE_OUT_OF_BOUNDS' USING ERRCODE = '23514';
    END IF;
    -- Missing parents are rejected by the ownership FKs, not accepted as valid sources.
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_pdf_kg_unit_guard BEFORE INSERT OR UPDATE OR DELETE ON kg_extraction_units
FOR EACH ROW EXECUTE FUNCTION pdf_kg_m0_guard_unit();

CREATE FUNCTION pdf_kg_m0_guard_chunk_set() RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.sealed_at IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM documents WHERE id = OLD.document_id AND deletion_status = 'deleting'
        ) THEN
            RAISE EXCEPTION 'PDF_KG_SEALED_SET_DELETE' USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    IF ROW(NEW.id, NEW.document_id, NEW.source_version, NEW.graph_build_id, NEW.segmentation_version,
           NEW.segmentation_config, NEW.segmentation_config_sha256, NEW.embedding_fingerprint, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.document_id, OLD.source_version, OLD.graph_build_id, OLD.segmentation_version,
           OLD.segmentation_config, OLD.segmentation_config_sha256, OLD.embedding_fingerprint, OLD.created_at)
       OR (OLD.sealed_at IS NOT NULL AND
           ROW(NEW.sealed_at, NEW.chunk_count, NEW.manifest_object_key, NEW.manifest_sha256) IS DISTINCT FROM
           ROW(OLD.sealed_at, OLD.chunk_count, OLD.manifest_object_key, OLD.manifest_sha256))
       OR (OLD.status = 'indexed' AND NEW IS DISTINCT FROM OLD) THEN
        RAISE EXCEPTION 'PDF_KG_SET_IDENTITY_OR_MANIFEST_IMMUTABLE' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_pdf_kg_set_immutable BEFORE UPDATE OR DELETE ON document_chunk_sets
FOR EACH ROW EXECUTE FUNCTION pdf_kg_m0_guard_chunk_set();

CREATE FUNCTION pdf_kg_m0_guard_chunk() RETURNS trigger LANGUAGE plpgsql AS $guard$
DECLARE
    parent_id uuid;
    parent_sealed timestamptz;
    parent_status varchar;
    source_length bigint;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.id, NEW.document_id, NEW.parse_run_id, NEW.chunk_set_id, NEW.source_version)
           IS DISTINCT FROM ROW(OLD.id, OLD.document_id, OLD.parse_run_id, OLD.chunk_set_id, OLD.source_version)
           AND (NEW.chunk_set_id IS NOT NULL OR OLD.chunk_set_id IS NOT NULL) THEN
            RAISE EXCEPTION 'PDF_KG_CHUNK_IDENTITY_IMMUTABLE' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN parent_id := OLD.chunk_set_id; ELSE parent_id := NEW.chunk_set_id; END IF;
    -- Existing legacy rows remain on their existing contract.
    IF parent_id IS NULL THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;
    SELECT sealed_at, status INTO parent_sealed, parent_status FROM document_chunk_sets WHERE id = parent_id FOR UPDATE;
    IF parent_sealed IS NOT NULL THEN
        IF TG_OP = 'INSERT' THEN
            RAISE EXCEPTION 'PDF_KG_SEALED_SET_INSERT' USING ERRCODE = '23514';
        ELSIF TG_OP = 'DELETE' THEN
            IF NOT EXISTS (SELECT 1 FROM documents WHERE id = OLD.document_id AND deletion_status = 'deleting') THEN
                RAISE EXCEPTION 'PDF_KG_SEALED_CHUNK_DELETE' USING ERRCODE = '23514';
            END IF;
        ELSIF ROW(NEW.content, NEW.content_sha256, NEW.source_start, NEW.source_end, NEW.chunk_index,
                  NEW.source_metadata, NEW.page_start, NEW.page_end, NEW.section_title, NEW.chunk_type,
                  NEW.chunk_method, NEW.content_format, NEW.token_count, NEW.created_at)
              IS DISTINCT FROM
              ROW(OLD.content, OLD.content_sha256, OLD.source_start, OLD.source_end, OLD.chunk_index,
                  OLD.source_metadata, OLD.page_start, OLD.page_end, OLD.section_title, OLD.chunk_type,
                  OLD.chunk_method, OLD.content_format, OLD.token_count, OLD.created_at)
              OR (parent_status = 'indexed' AND NEW IS DISTINCT FROM OLD) THEN
            RAISE EXCEPTION 'PDF_KG_SEALED_CHUNK_CONTENT_IMMUTABLE' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    SELECT character_count INTO source_length FROM document_source_versions WHERE source_version = NEW.source_version;
    IF NEW.source_end > source_length THEN
        RAISE EXCEPTION 'PDF_KG_CHUNK_RANGE_OUT_OF_BOUNDS' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_pdf_kg_chunk_guard BEFORE INSERT OR UPDATE OR DELETE ON document_chunks
FOR EACH ROW EXECUTE FUNCTION pdf_kg_m0_guard_chunk();

CREATE FUNCTION pdf_kg_m0_guard_publication() RETURNS trigger LANGUAGE plpgsql AS $guard$
DECLARE
    set_status varchar;
    build_status varchar;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.current_chunk_set_id IS DISTINCT FROM OLD.current_chunk_set_id THEN
            IF NEW.publication_revision <> OLD.publication_revision + 1 THEN
                RAISE EXCEPTION 'PDF_KG_PUBLICATION_REVISION_MUST_ADVANCE' USING ERRCODE = '23514';
            END IF;
        ELSIF NEW.publication_revision IS DISTINCT FROM OLD.publication_revision THEN
            RAISE EXCEPTION 'PDF_KG_PUBLICATION_REVISION_WITHOUT_SWITCH' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF NEW.current_chunk_set_id IS NOT NULL THEN
        SELECT s.status, g.status INTO set_status, build_status
        FROM document_chunk_sets s JOIN document_graph_builds g ON g.id = s.graph_build_id
        WHERE s.id = NEW.current_chunk_set_id AND s.document_id = NEW.id;
        IF NEW.deletion_status <> 'normal' OR set_status IS DISTINCT FROM 'indexed'
           OR build_status IS NULL OR build_status NOT IN ('ready', 'ready_empty') THEN
            RAISE EXCEPTION 'PDF_KG_PUBLICATION_NOT_READY' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_pdf_kg_publication_guard BEFORE INSERT OR UPDATE OF current_chunk_set_id, publication_revision ON documents
FOR EACH ROW EXECUTE FUNCTION pdf_kg_m0_guard_publication();

CREATE FUNCTION pdf_kg_m0_guard_job() RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
    IF ROW(NEW.id, NEW.document_id, NEW.operation, NEW.request_id, NEW.input_fingerprint, NEW.created_at)
       IS DISTINCT FROM ROW(OLD.id, OLD.document_id, OLD.operation, OLD.request_id, OLD.input_fingerprint, OLD.created_at)
       OR (OLD.source_version IS NOT NULL AND NEW.source_version IS DISTINCT FROM OLD.source_version)
       OR (OLD.graph_build_id IS NOT NULL AND NEW.graph_build_id IS DISTINCT FROM OLD.graph_build_id)
       OR (OLD.chunk_set_id IS NOT NULL AND NEW.chunk_set_id IS DISTINCT FROM OLD.chunk_set_id)
       OR (OLD.status IN ('succeeded', 'cancelled') AND NEW IS DISTINCT FROM OLD) THEN
        RAISE EXCEPTION 'PDF_KG_JOB_IDENTITY_OR_RESULT_IMMUTABLE' USING ERRCODE = '23514';
    END IF;
    IF NEW.fencing_token < OLD.fencing_token OR NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'PDF_KG_JOB_COUNTER_REGRESSION' USING ERRCODE = '23514';
    END IF;
    IF NEW.status = 'running' AND (OLD.status <> 'running' OR NEW.lease_token IS DISTINCT FROM OLD.lease_token) THEN
        IF NEW.fencing_token <= OLD.fencing_token OR NEW.attempt_count <> OLD.attempt_count + 1
           OR NEW.lease_token IS NOT DISTINCT FROM OLD.lease_token THEN
            RAISE EXCEPTION 'PDF_KG_JOB_NEW_LEASE_REQUIRES_FENCE' USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.status = 'running' AND
          (NEW.fencing_token <> OLD.fencing_token OR NEW.attempt_count <> OLD.attempt_count
           OR NEW.locked_by IS DISTINCT FROM OLD.locked_by OR NEW.locked_at IS DISTINCT FROM OLD.locked_at) THEN
        RAISE EXCEPTION 'PDF_KG_JOB_RENEWAL_IDENTITY_CHANGED' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_pdf_kg_job_guard BEFORE UPDATE ON document_processing_jobs
FOR EACH ROW EXECUTE FUNCTION pdf_kg_m0_guard_job();
"""


def upgrade() -> None:
    op.execute(TABLE_DDL)
    op.execute(GUARD_DDL)


def downgrade() -> None:
    raise RuntimeError(
        "0013 retains frozen sources, graph identities, chunks and audit jobs. "
        "Roll back application deployment and retain schema/data; manual recovery requires an approved backup plan."
    )
