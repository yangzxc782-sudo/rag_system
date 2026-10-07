"""Offline M2 service integration. SQLite checks flow, NOT PostgreSQL triggers/locks."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import CheckConstraint, DefaultClause, JSON, MetaData, create_engine, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BusinessError
from app.db.base import Base
from app.ingestion.frozen_source import json_bytes, render_frozen_source, sha256_bytes
from app.models import Document, DocumentChunk, DocumentParseRun, GraphBuild, KGExtractionUnit, SourceDocumentVersion, DocumentProcessingJob
from app.services import document_graph_builds as service
from test_frozen_source import block
from test_kg_v2_protocol_units import raw_part


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    local = MetaData()
    for table in Base.metadata.sorted_tables:
        copied = table.to_metadata(local)
        for constraint in tuple(copied.constraints):
            if isinstance(constraint, CheckConstraint):
                # PG-only DDL/guards are covered by the separately gated M0 suite.
                copied.constraints.remove(constraint)
        for index in tuple(copied.indexes):
            if index.dialect_options["postgresql"].get("where") is not None:
                index.dialect_options["sqlite"]["where"] = index.dialect_options["postgresql"]["where"]
        for column in copied.columns:
            if isinstance(column.type, (JSONB, VECTOR)):
                column.type = JSON()
            if column.server_default is not None and "::jsonb" in str(column.server_default.arg):
                column.server_default = DefaultClause(text("'{}'"))
    local.create_all(engine)
    with Session(engine, autoflush=False) as session:
        yield session
    engine.dispose()


@pytest.fixture
def settings():
    return Settings(_env_file=None, kg_build_enabled=True, llm_provider="local", llm_model="synthetic-model",
                    llm_base_url="http://localhost:11434/v1")


class MemoryAssets(service.GraphAssets):
    def __init__(self, db):
        self.db, self.data, self.events = db, {}, []

    def read(self, bucket, key, digest, *, limit=32_000_000):
        assert not self.db.in_transaction(), "storage read inside SQL transaction"
        self.events.append(("read", key))
        content = self.data[key]
        if sha256_bytes(content) != digest:
            raise ValueError("hash mismatch")
        return content

    def save(self, bucket, prefix, data):
        assert not self.db.in_transaction(), "storage write inside SQL transaction"
        content = json_bytes(data)
        digest = sha256_bytes(content)
        key = f"{prefix}/{digest}.json"
        if key in self.data:
            assert self.data[key] == content
        self.data[key] = content
        self.events.append(("save", key))
        return key, digest


class Provider:
    def __init__(self, db, values=None):
        self.db, self.calls, self.values, self.hook = db, [], list(values or []), None

    def generate(self, request):
        assert not self.db.in_transaction(), "model call inside SQL transaction"
        self.calls.append(request)
        if self.hook:
            self.hook()
        value = self.values.pop(0) if self.values else raw_part()
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(text=json_bytes(value).decode("utf-8"))


class Writer:
    def __init__(self, db):
        self.db, self.calls, self.hook, self.fail = db, [], None, False

    def write(self, payload):
        assert not self.db.in_transaction(), "Neo4j call inside SQL transaction"
        self.calls.append(deepcopy(payload))
        if self.hook:
            self.hook()
        if self.fail:
            raise TimeoutError("sensitive connection details")
        return dict(payload_sha256=sha256_bytes(json_bytes(payload)), entities=len(payload["entities"]),
                    relationships=len(payload["relationships"]), graph_build_id=payload["graph_build_id"], status="built")


def source(db, assets, text="# 6 技术要求\nZL101 ≥350 MPa", filename="test.pdf"):
    document_id, parse_id, version = uuid4(), uuid4(), uuid4()
    item = Document(id=document_id, original_filename=filename, file_type=".pdf", mime_type="application/pdf",
                    bucket_name="synthetic", object_key=f"raw/{document_id}.pdf", process_status="cleaned_source_ready")
    run = DocumentParseRun(id=parse_id, document_id=document_id, parser_provider="mineru_api")
    db.add(item)
    db.flush()
    db.add(run)
    b = block(0, text)
    b.document_id, b.parse_run_id = document_id, parse_id
    frozen = render_frozen_source([b], document_id=document_id, parse_run_id=parse_id, source_version=version, output_prefix="x")
    key, map_key = f"source/{version}/cleaned.md", f"source/{version}/map.json"
    assets.data[key], assets.data[map_key] = frozen.canonical, frozen.block_map
    row = SourceDocumentVersion(source_version=version, document_id=document_id, parse_run_id=parse_id,
        bucket_name="synthetic", canonical_object_key=key, canonical_sha256=frozen.canonical_sha256,
        character_count=frozen.character_count, block_map_object_key=map_key, block_map_sha256=frozen.block_map_sha256,
        cleaner_version="v1", renderer_version="pdf-canonical-v1", cleaning_config_sha256="a" * 64)
    db.add(row)
    db.commit()
    return document_id, version


def prepared(db, settings, text="# 6 技术要求\nZL101 ≥350 MPa", filename="test.pdf"):
    assets = MemoryAssets(db)
    doc, version = source(db, assets, text, filename)
    request = uuid4()
    result = service.prepare_graph_build(db, doc, version, request, settings=settings, assets=assets)
    return doc, version, result["graph_build_id"], assets, request


def advance(db, settings, doc, build, assets, provider, writer, **kwargs):
    return service.advance_graph_build(db, doc, build, settings=settings, assets=assets, provider=provider, writer=writer, **kwargs)


@pytest.mark.parametrize("budget", [8192, 4096])
def test_m2_request_uses_explicit_kg_output_budget(db, settings, budget):
    settings.llm_max_tokens, settings.kg_llm_max_tokens = 1024, budget
    doc, _, build, assets, _ = prepared(db, settings)
    provider, writer = Provider(db), Writer(db)
    advance(db, settings, doc, build, assets, provider, writer)
    request, = provider.calls
    assert request.max_tokens == budget and settings.llm_max_tokens == 1024
    assert request.temperature == 0.1 and request.json_mode is True
    assert request.timeout_seconds == settings.kg_model_timeout_seconds
    assert not request.tools and request.stop is None and request.think is None
    assert db.get(GraphBuild, build).provider_fingerprint == service.provider_fingerprint(settings)
    assert not writer.calls and not db.scalar(select(DocumentChunk.id))


@pytest.mark.parametrize("provider", ["local", "api"])
def test_kg_fingerprint_hashes_effective_budget_not_generic_default(settings, provider):
    settings = settings.model_copy(update=dict(llm_provider=provider, llm_max_tokens=1024,
        kg_llm_max_tokens=8192, llm_remote_base_url="https://remote.example.invalid/v1",
        llm_remote_model="synthetic-remote", llm_remote_api_key="test-key-not-real"))
    fingerprint = service.provider_fingerprint(settings)
    # Keep the existing canonical JSON/SHA-256 contract and payload key.
    expected = dict(provider=provider,
        model=settings.llm_model if provider == "local" else settings.llm_remote_model,
        endpoint=settings.llm_base_url if provider == "local" else settings.llm_remote_base_url,
        extraction_version=service.EXTRACTION_VERSION, temperature=0.1, max_tokens=8192,
        json_mode=True, timeout=settings.kg_model_timeout_seconds)
    assert fingerprint == sha256_bytes(json_bytes(expected))
    assert fingerprint != service.provider_fingerprint(settings.model_copy(update={"kg_llm_max_tokens": 4096}))
    assert fingerprint == service.provider_fingerprint(settings.model_copy(update={"llm_max_tokens": 2048}))


def test_old_1024_build_cannot_retry_under_new_kg_budget(db, settings):
    settings.llm_max_tokens = settings.kg_llm_max_tokens = 1024
    body = "\n".join(f"# {i}\nSynthetic clause {i}" for i in range(1, 5))
    doc, _, build, assets, _ = prepared(db, settings, body)
    legacy_payload = dict(provider="local", model=settings.llm_model, endpoint=settings.llm_base_url,
        extraction_version=service.EXTRACTION_VERSION, temperature=0.1, max_tokens=settings.llm_max_tokens,
        json_mode=True, timeout=settings.kg_model_timeout_seconds)
    assert db.get(GraphBuild, build).provider_fingerprint == sha256_bytes(json_bytes(legacy_payload))
    provider = Provider(db, [dict(entities=[], relationships=[])] * 3 + [BusinessError("LLM_JSON_INVALID", "synthetic")])
    writer = Writer(db)
    for _ in range(3):
        advance(db, settings, doc, build, assets, provider, writer)
    with pytest.raises(BusinessError) as failed:
        advance(db, settings, doc, build, assets, provider, writer)
    assert failed.value.code == "KG_EXTRACTION_FAILED"
    assert db.get(GraphBuild, build).status == "partial_failed"
    job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.graph_build_id == build))
    before = deepcopy([service._snapshot(row) for row in
        [db.get(GraphBuild, build), job, *service._units(db, build)]])
    previous_assets, previous_events = deepcopy(assets.data), list(assets.events)
    settings.kg_llm_max_tokens = 8192
    with pytest.raises(BusinessError) as caught:
        advance(db, settings, doc, build, assets, provider, writer, retry=True)
    assert caught.value.code == "KG_BUILD_INPUT_CHANGED"
    db.rollback()
    assert [service._snapshot(row) for row in [db.get(GraphBuild, build), job, *service._units(db, build)]] == before
    assert len(provider.calls) == 4 and not writer.calls
    assert assets.data == previous_assets and assets.events == previous_events
    assert not db.scalar(select(DocumentChunk.id))


def test_complete_graph_stage_stops_before_chunks_and_is_idempotent(db, settings):
    doc, version, build, assets, request = prepared(db, settings)
    provider, writer = Provider(db), Writer(db)
    assert service.prepare_graph_build(db, doc, version, request, settings=settings, assets=assets)["graph_build_id"] == build
    first = advance(db, settings, doc, build, assets, provider, writer)
    assert first["unit_counts"] == {"succeeded_nonempty": 1} and not writer.calls
    ready = advance(db, settings, doc, build, assets, provider, writer)
    assert ready["status"] == "ready" and ready["stage"] == "kg_ready" and ready["job_status"] == "queued"
    assert not ready["can_advance"] and ready["next_stage"] == "chunking"
    index = service.load_anchor_index(db, doc, version, build)
    assert len(index) == 1 and index[0]["anchor_metadata"]["anchor_type"] == "clause"
    assert "table_ref" not in index[0]["anchor_metadata"]
    again = advance(db, settings, doc, build, assets, provider, writer)
    assert again == ready and len(provider.calls) == len(writer.calls) == 1
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0


@pytest.mark.parametrize("body", ["# 无抽取单元", "# 正文\n没有知识"])
def test_successful_empty_build_skips_writer_and_retains_unit_identity(db, settings, body):
    doc, version, build, assets, _ = prepared(db, settings, body)
    provider, writer = Provider(db, [dict(entities=[], relationships=[])]), Writer(db)
    result = advance(db, settings, doc, build, assets, provider, writer)
    if result["status"] != "ready_empty":
        result = advance(db, settings, doc, build, assets, provider, writer)
    assert result["status"] == "ready_empty" and result["anchor_count"] == 0 and not writer.calls
    assert service.load_anchor_index(db, doc, version, build) == ()
    for unit in db.scalars(select(KGExtractionUnit)):
        assert unit.allocated_anchor_id and unit.result_sha256 and not unit.has_qualified_triples


def test_model_error_is_failed_not_empty_and_retry_is_explicit(db, settings):
    doc, _, build, assets, _ = prepared(db, settings)
    provider, writer = Provider(db, [TimeoutError("token secret")]), Writer(db)
    with pytest.raises(BusinessError) as error:
        advance(db, settings, doc, build, assets, provider, writer)
    assert error.value.code == "KG_EXTRACTION_FAILED" and "secret" not in str(error.value)
    status = service.graph_build_status(db, doc, build)
    assert status["status"] == "extraction_failed" and status["unit_counts"] == {"failed": 1}
    with pytest.raises(BusinessError, match="知识图谱"):
        advance(db, settings, doc, build, assets, provider, writer)
    db.rollback()
    advance(db, settings, doc, build, assets, provider, writer, retry=True)
    assert not writer.calls
    assert advance(db, settings, doc, build, assets, provider, writer)["status"] == "ready"


@pytest.mark.parametrize("cause_code,logged_code", [
    ("LLM_JSON_INVALID", "LLM_JSON_INVALID"), ("SECRET_UNRECOGNIZED_CODE", "UNCLASSIFIED"),
])
def test_json_failure_diagnostics_keep_prior_empty_units_and_stage_contract(
    db, settings, caplog, cause_code, logged_code,
):
    body = "\n".join(f"# {i}\nSECRET_PDF_SOURCE_{i}" for i in range(1, 6))
    doc, _, build, assets, _ = prepared(db, settings, body)
    failure = BusinessError(cause_code, "SECRET_EXCEPTION_BODY", detail={"source": body})
    provider = Provider(db, [dict(entities=[], relationships=[])] * 3 + [failure])
    writer = Writer(db)
    for _ in range(3):
        advance(db, settings, doc, build, assets, provider, writer)
    before = [deepcopy(service._snapshot(u)) for u in service._units(db, build)[:3]]
    with pytest.raises(BusinessError) as caught:
        advance(db, settings, doc, build, assets, provider, writer)
    assert caught.value.code == "KG_EXTRACTION_FAILED" and caught.value.__cause__ is failure
    units = service._units(db, build)
    assert [service._snapshot(u) for u in units[:3]] == before
    assert all(u.status == "succeeded_empty" and u.piece_checkpoints for u in units[:3])
    assert units[3].status == "failed" and units[3].piece_checkpoints == {}
    assert units[3].last_error_code == "KG_EXTRACTION_FAILED" and units[4].status == "pending"
    assert db.get(GraphBuild, build).status == "partial_failed"
    job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.graph_build_id == build))
    assert job.status == "failed" and job.stage == "kg_extracting" and job.last_error_code == "KG_EXTRACTION_FAILED"
    assert job.chunk_set_id is None and not writer.calls and len(provider.calls) == 4
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0
    record, = [r for r in caplog.records if getattr(r, "event", None) == "kg_extraction_unit_failed"]
    assert record.document_id == str(doc) and record.graph_build_id == str(build)
    assert record.unit_id == str(units[3].id) and record.unit_index == 3 and record.unit_kind == "clause"
    assert record.cause_code == logged_code
    rendered = caplog.text + repr(record.__dict__)
    assert "SECRET" not in rendered and body not in rendered and record.exc_info is None


def test_table_piece_retry_reuses_completed_piece_and_anchor(db, settings):
    body = "# 4\n表3\n|牌号|性能|\n|---|---|\n" + "\n".join(f"|ZL{i}|≥350 MPa|" for i in range(41))
    doc, version, build, assets, _ = prepared(db, settings, body)
    provider, writer = Provider(db, [raw_part(), TimeoutError(), raw_part()]), Writer(db)
    advance(db, settings, doc, build, assets, provider, writer)
    with pytest.raises(BusinessError):
        advance(db, settings, doc, build, assets, provider, writer)
    assert service.graph_build_status(db, doc, build)["status"] == "partial_failed"
    advance(db, settings, doc, build, assets, provider, writer, retry=True)
    assert len(provider.calls) == 3
    advance(db, settings, doc, build, assets, provider, writer)
    index, = service.load_anchor_index(db, doc, version, build)
    assert index["anchor_metadata"]["table_ref"] == "T-4-1"
    assert len(writer.calls[0]["units"]) == 1 and len(writer.calls[0]["relationships"]) == 2


def test_write_failure_never_seals_and_retry_never_recalls_model(db, settings):
    doc, version, build, assets, _ = prepared(db, settings)
    provider, writer = Provider(db), Writer(db)
    advance(db, settings, doc, build, assets, provider, writer)
    writer.fail = True
    with pytest.raises(BusinessError):
        advance(db, settings, doc, build, assets, provider, writer)
    with pytest.raises(BusinessError) as error:
        service.load_anchor_index(db, doc, version, build)
    assert error.value.code == "KG_BUILD_NOT_READY"
    db.rollback()
    writer.fail = False
    result = advance(db, settings, doc, build, assets, provider, writer, retry=True)
    assert result["status"] == "ready" and len(provider.calls) == 1
    assert writer.calls[0] == writer.calls[1]


def test_expired_lease_takeover_fences_late_model_result(db, settings):
    doc, _, build, assets, _ = prepared(db, settings)
    provider, writer = Provider(db), Writer(db)
    def steal():
        job = db.scalar(select(DocumentProcessingJob))
        job.lease_token, job.fencing_token = uuid4(), job.fencing_token + 1
        db.commit()
    provider.hook = steal
    with pytest.raises(BusinessError) as error:
        advance(db, settings, doc, build, assets, provider, writer)
    assert error.value.code == "KG_LEASE_LOST"
    job = db.scalar(select(DocumentProcessingJob))
    assert job.status == "running" and job.last_error_code is None
    job.lease_expires_at = service._now(db) - timedelta(seconds=1)
    db.commit()
    provider.hook = None
    with pytest.raises(BusinessError) as error:
        advance(db, settings, doc, build, assets, provider, writer)
    assert error.value.code == "KG_EXPLICIT_RETRY_REQUIRED"
    db.rollback()
    assert advance(db, settings, doc, build, assets, provider, writer, retry=True)["unit_counts"] == {"succeeded_nonempty": 1}


def test_deletion_guard_rechecked_after_model_before_commit(db, settings):
    doc, _, build, assets, _ = prepared(db, settings)
    provider, writer = Provider(db), Writer(db)
    def delete():
        db.get(Document, doc).deletion_status = "deleting"
        db.commit()
    provider.hook = delete
    with pytest.raises(BusinessError):
        advance(db, settings, doc, build, assets, provider, writer)
    assert not writer.calls
    unit = db.scalar(select(KGExtractionUnit))
    assert not unit.has_qualified_triples and unit.result_object_key is None


def test_same_day_chinese_slug_conflict_preserves_previous_build(db, settings):
    doc, version, build, assets, request = prepared(db, settings, filename="中文甲.pdf")
    other_doc, other_version = source(db, assets, filename="中文乙.pdf")
    with pytest.raises(BusinessError) as error:
        service.prepare_graph_build(db, other_doc, other_version, uuid4(), settings=settings, assets=assets)
    assert error.value.code == "KG_GRAPH_ID_CONFLICT"
    assert db.scalar(select(func.count()).select_from(GraphBuild)) == 1
    db.rollback()
    with pytest.raises(BusinessError) as error:
        service.prepare_graph_build(db, doc, other_version, request, settings=settings, assets=assets)
    assert error.value.code == "KG_REQUEST_ID_CONFLICT"


def test_source_corruption_and_changed_provider_refuse_reuse(db, settings):
    doc, _, build, assets, _ = prepared(db, settings)
    provider, writer = Provider(db), Writer(db)
    settings.llm_model = "changed-model"
    with pytest.raises(BusinessError) as error:
        advance(db, settings, doc, build, assets, provider, writer)
    assert error.value.code == "KG_BUILD_INPUT_CHANGED"
    db.rollback()
    settings.llm_model = "synthetic-model"
    key = next(k for k in assets.data if k.endswith("cleaned.md"))
    assets.data[key] += b"changed"
    with pytest.raises(BusinessError):
        advance(db, settings, doc, build, assets, provider, writer)
    assert not provider.calls and not writer.calls


def test_disabled_build_performs_no_external_io(db, settings):
    settings.kg_build_enabled = False
    with pytest.raises(BusinessError) as error:
        service.prepare_graph_build(db, uuid4(), uuid4(), uuid4(), settings=settings)
    assert error.value.code == "KG_BUILD_DISABLED"


def test_asset_write_requires_hash_verified_readback(monkeypatch):
    saved = {}
    monkeypatch.setattr(service, "upload_bytes_to_minio", lambda **kw: saved.update({kw["object_key"]: kw["content"]}))
    monkeypatch.setattr(service, "get_object_bytes_from_minio", lambda **kw: saved[kw["object_key"]])
    assets = service.GraphAssets()
    key, digest = assets.save("synthetic", "kg-assets/owned", {"unit_id": "synthetic"})
    assert key.endswith(digest + ".json")
    monkeypatch.setattr(service, "get_object_bytes_from_minio", lambda **kw: b"corrupt")
    with pytest.raises(ValueError, match="hash"):
        assets.save("synthetic", "kg-assets/owned", {"unit_id": "synthetic"})


def test_sql_seal_failure_after_successful_writer_is_recoverable(db, settings, monkeypatch):
    doc, _, build, assets, _ = prepared(db, settings)
    provider, writer = Provider(db), Writer(db)
    advance(db, settings, doc, build, assets, provider, writer)
    commit = db.commit
    def fail_seal():
        if any(isinstance(row, GraphBuild) and row.status == "ready" for row in db.dirty):
            raise RuntimeError("synthetic commit failure")
        commit()
    monkeypatch.setattr(db, "commit", fail_seal)
    with pytest.raises(BusinessError):
        advance(db, settings, doc, build, assets, provider, writer)
    assert service.graph_build_status(db, doc, build)["status"] == "write_failed"
    monkeypatch.setattr(db, "commit", commit)
    assert advance(db, settings, doc, build, assets, provider, writer, retry=True)["status"] == "ready"
    assert len(provider.calls) == 1 and writer.calls[0] == writer.calls[1]


def test_foreign_document_cannot_read_advance_or_load_index(db, settings):
    doc, version, build, assets, _ = prepared(db, settings)
    other, _ = source(db, assets, filename="other.pdf")
    for operation in (
        lambda: service.graph_build_status(db, other, build),
        lambda: advance(db, settings, other, build, assets, Provider(db), Writer(db)),
        lambda: service.load_anchor_index(db, other, version, build),
    ):
        with pytest.raises(BusinessError):
            operation()
        db.rollback()


def test_busy_lease_does_not_call_model_or_overwrite(db, settings):
    doc, _, build, assets, _ = prepared(db, settings)
    provider, writer = Provider(db), Writer(db)
    def concurrent_request():
        job = db.scalar(select(DocumentProcessingJob))
        token, fence = job.lease_token, job.fencing_token
        db.commit()
        with pytest.raises(BusinessError) as error:
            advance(db, settings, doc, build, assets, Provider(db), writer, retry=True)
        assert error.value.code == "KG_BUILD_BUSY"
        db.rollback()
        job = db.scalar(select(DocumentProcessingJob))
        assert (job.lease_token, job.fencing_token) == (token, fence)
        db.commit()
    provider.hook = concurrent_request
    advance(db, settings, doc, build, assets, provider, writer)
    assert len(provider.calls) == 1 and not writer.calls


def test_piece_checkpoint_hash_corruption_blocks_publication(db, settings):
    text = '|牌号|性能|\n|---|---|\n' + '\n'.join(f'|ZL{i}|350 MPa|' for i in range(41))
    doc, _, build, assets, _ = prepared(db, settings, text)
    provider, writer = Provider(db), Writer(db)
    advance(db, settings, doc, build, assets, provider, writer)
    key = next(k for k in assets.data if '/pieces/' in k)
    assets.data[key] += b'corrupt'
    with pytest.raises(BusinessError):
        advance(db, settings, doc, build, assets, provider, writer)
    assert service.graph_build_status(db, doc, build)["status"] == "partial_failed"
    assert not writer.calls and len(provider.calls) == 1


def test_extraction_budget_refuses_before_preallocation_or_model(db, settings):
    assets = MemoryAssets(db)
    doc, version = source(db, assets, "# 一\n甲\n# 二\n乙")
    settings.kg_max_units = 1
    with pytest.raises(BusinessError) as error:
        service.prepare_graph_build(db, doc, version, uuid4(), settings=settings, assets=assets)
    assert error.value.code == "KG_EXTRACTION_BUDGET_EXCEEDED"
    assert not db.scalar(select(GraphBuild.id))


@pytest.mark.parametrize("status", ["kg_extracting", "kg_writing", "kg_failed", "kg_ready", "kg_ready_empty"])
def test_running_kg_job_blocks_deletion_before_manifest(db, settings, status, monkeypatch):
    from app.services import document_deletion
    from unittest.mock import Mock
    doc, _, _, _, _ = prepared(db, settings)
    db.get(Document, doc).process_status = status
    job = db.scalar(select(DocumentProcessingJob))
    job.status = "running"
    job.locked_by, job.lease_token = "synthetic", uuid4()
    job.fencing_token = job.attempt_count = 1
    job.locked_at, job.lease_expires_at = service._now(db), service._now(db) + timedelta(seconds=600)
    db.commit()
    manifest = Mock(side_effect=AssertionError("no manifest/external deletes for frozen source"))
    monkeypatch.setattr(document_deletion, "build_document_deletion_manifest", manifest)
    with pytest.raises(BusinessError) as error:
        document_deletion.request_document_deletion(db, document_id=doc, settings=SimpleNamespace(document_deletion_executor_enabled=True))
    assert error.value.code == "DOCUMENT_PROCESSING_IN_PROGRESS" and not manifest.called
