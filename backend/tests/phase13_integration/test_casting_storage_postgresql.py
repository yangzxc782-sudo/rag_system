"""Real isolated PostgreSQL and engine; object storage is an explicit memory double."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.api.v1.casting_design import router
from app.casting.execution_protocol import CastingExecutionError, digest
from app.db.langgraph import CheckpointPool, CheckpointUnavailable
from app.local_server import LocalConversationTransport
from app.models.casting_design_file import CastingDesignFile
from app.models.casting_design_run import CastingDesignRun
from app.models.qa_turn import QATurn
from app.schemas.casting_storage import CastingStorageError
from app.services.casting_design import CastingDesignService
from app.services.casting_engine import CastingEngine
from app.services.casting_files import CastingFiles, CastingObjectStorage
from app.services.casting_repository import CastingRepository
from app.services.casting_rules import ASSETS
from app.services.conversation_repository import ConversationRepository
from phase13_m2_support import call, database_engine, factory, session, settings_for
from phase13_support import migration

pytestmark = pytest.mark.phase13_integration
BACKEND = Path(__file__).resolve().parents[2]
RAW = (ASSETS / "vendor/v5_1/input-v1.json").read_bytes()


class MemoryStorage:
    def __init__(self):
        self.objects = {}
        self.fail_put = False

    def put(self, row, raw):
        if self.fail_put:
            raise OSError("PRIVATE STORAGE EXCEPTION")
        self.objects[(row.bucket, row.object_key)] = raw

    def get(self, row):
        return self.objects[(row.bucket, row.object_key)]


@pytest.fixture(scope="module")
def casting_db(phase13_root_engine):
    engine = database_engine(phase13_root_engine, revision="0011_casting_storage")
    yield engine
    engine.dispose()


@pytest.fixture
def stack(casting_db, tmp_path):
    files = CastingFiles(factory(casting_db), MemoryStorage(), bucket="isolated-casting-test")
    python = Path(os.environ.get("CASTING_TEST_PYTHON", BACKEND / ".venv-casting" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python")))
    if not python.is_file():
        pytest.fail("This integration suite requires the separately pinned casting interpreter")
    service = CastingDesignService(files, CastingEngine(python_executable=python, work_root=tmp_path / "runs"))
    return service, session(casting_db)


def accept(engine, sid, fid=None, rid=None):
    return call(engine, lambda repo: repo.start_turn(sid, rid or uuid4(), "计算浇冒系统", casting_enabled=True,
                casting_input_file_id=fid, graph_version="phase13_m3_v2"))


def finish_turn(engine, turn):
    call(engine, lambda repo: repo.transition_turn(turn.session_id, turn.id, expected_status="running",
        new_status="failed", expected_attempt=1, error_code="QA_SYNTHETIC_FINISHED"))


def client(service, *, enabled=True, wrapped=True, peer="127.0.0.1"):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.settings = SimpleNamespace(conversation_enabled=True, casting_design_enabled=enabled)
    app.state.conversations = object()
    app.state.casting_design = service
    return TestClient(LocalConversationTransport(app) if wrapped else app,
                      base_url="http://127.0.0.1:8000", client=(peer, 12345))


def test_upgrade_preserves_legacy_turn_and_both_readiness_modes(phase13_root_engine):
    engine = database_engine(phase13_root_engine)
    try:
        sid = session(engine)
        old = call(engine, lambda repo: repo.start_turn(sid, uuid4(), "旧会话"))
        # The newly mapped deferred columns must never leak into 0010 SQL.
        assert call(engine, lambda repo: repo.get_turn(sid, old.id)).question == "旧会话"
        pool = CheckpointPool(settings_for(engine))
        pool.open()
        pool.close()
        pool = CheckpointPool(settings_for(engine, conversation_enabled=True, casting_design_enabled=True))
        with pytest.raises(CheckpointUnavailable):
            pool.open()
        with engine.begin() as db:
            migration(db, "0011_casting_storage")
        pool.open()
        pool.close()
        assert call(engine, lambda repo: repo.get_turn(sid, old.id)).request_fingerprint == old.request_fingerprint
        with factory(engine)() as db:
            row = db.get(QATurn, old.id)
            assert row.effective_casting_input_file_id is None and row.graph_version is None
        from app.db.base import Base
        for name in ("casting_design_files", "casting_design_runs"):
            actual = {c["name"] for c in inspect(engine).get_columns(name)}
            assert actual == set(Base.metadata.tables[name].columns.keys())
    finally:
        engine.dispose()


def test_upload_idempotency_isolation_and_no_document_rows(casting_db, stack):
    service, sid = stack
    rid = uuid4()
    with casting_db.connect() as db:
        before = db.execute(text("SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM document_chunks)")).one()
    one = service.files.upload(sid, rid, "input.json", RAW)
    two = service.files.upload(sid, rid, "input.json", RAW)
    assert one.file_id == two.file_id and one.sha256 == digest(RAW) and one.storage_state == "ready"
    assert not one.admission_passed
    with pytest.raises(CastingStorageError, match="request_id"):
        service.files.upload(sid, rid, "another.json", RAW)
    other = session(casting_db)
    with pytest.raises(CastingStorageError) as caught:
        service.files.input_bytes(other, one.file_id)
    assert caught.value.status_code == 404
    assert service.files.list_inputs(other).items == []
    with casting_db.connect() as db:
        assert db.execute(text("SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM document_chunks)")).one() == before


@pytest.mark.parametrize("window", ["put", "commit"])
def test_upload_compensation_after_storage_or_commit_failure(stack, window):
    service, sid = stack
    files, rid = service.files, uuid4()
    if window == "put":
        files.storage.fail_put = True
    else:
        files.after_put = lambda _: (_ for _ in ()).throw(RuntimeError("injected commit boundary"))
    with pytest.raises((CastingStorageError, RuntimeError)):
        files.upload(sid, rid, "input.json", RAW)
    pending = files.list_inputs(sid).items[0]
    assert pending.storage_state == "pending"
    files.storage.fail_put, files.after_put = False, None
    saved = files.upload(sid, rid, "input.json", RAW)
    assert saved.file_id == pending.file_id and saved.storage_state == "ready"
    assert len(files.storage.objects) == 1


def test_parallel_same_upload_has_one_file(stack):
    service, sid = stack
    rid = uuid4()
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: service.files.upload(sid, rid, "input.json", RAW), range(2)))
    assert results[0].file_id == results[1].file_id
    assert len(service.files.list_inputs(sid).items) == 1


def test_selection_only_explicit_admitted_history_and_request_replay(casting_db, stack):
    service, sid = stack
    selected = service.files.upload(sid, uuid4(), "a.json", RAW)
    unused = service.files.upload(sid, uuid4(), "b.json", RAW)
    first = accept(casting_db, sid)
    assert first.effective_casting_input_file_id is None
    finish_turn(casting_db, first)
    explicit = accept(casting_db, sid, selected.file_id)
    assert explicit.effective_casting_input_file_id == selected.file_id
    finish_turn(casting_db, explicit)
    with factory(casting_db)() as db, db.begin():
        db.get(CastingDesignFile, selected.file_id).admission_passed = True  # synthetic history setup
    inherited = accept(casting_db, sid)
    assert inherited.effective_casting_input_file_id == selected.file_id != unused.file_id
    finish_turn(casting_db, inherited)
    with pytest.raises(CastingStorageError):
        accept(casting_db, sid, uuid4())  # Explicit invalid file cannot fall back.
    later = accept(casting_db, sid, unused.file_id)
    finish_turn(casting_db, later)
    replay = accept(casting_db, sid, rid=inherited.request_id)
    assert replay.id == inherited.id and replay.effective_casting_input_file_id == selected.file_id


def test_cross_session_foreign_keys(casting_db, stack):
    service, sid = stack
    file = service.files.upload(sid, uuid4(), "input.json", RAW)
    other = session(casting_db)
    turn = accept(casting_db, other)
    with pytest.raises(IntegrityError):
        with factory(casting_db)() as db, db.begin():
            db.get(QATurn, turn.id).effective_casting_input_file_id = file.file_id


@pytest.mark.casting_engine
def test_compute_persist_recover_without_recalculation_and_download(casting_db, stack, monkeypatch):
    service, sid = stack
    file = service.files.upload(sid, uuid4(), "input.json", RAW)
    turn = accept(casting_db, sid, file.file_id)
    service.before_finish = lambda _: (_ for _ in ()).throw(RuntimeError("injected DB commit loss"))
    with pytest.raises(RuntimeError):
        service.execute_turn(sid, turn.id)
    with factory(casting_db)() as db:
        row = db.scalar(select(CastingDesignRun).where(CastingDesignRun.turn_id == turn.id))
        assert row.status == "persisting"
        rid = row.id
    service.before_finish = None
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("must reuse existing calculation"))
    monkeypatch.setattr(service.engine.rules, "select", lambda *a, **kw: pytest.fail("must reuse frozen rules"))
    result = service.execute_turn(sid, turn.id)
    assert result.run_id == rid and result.status == "succeeded" and result.candidate_count == 4
    assert service.execute_turn(sid, turn.id).run_id == rid
    assert service.files.list_inputs(sid).items[0].admission_passed
    raw, sha = service.recommendation(sid, rid)
    assert digest(raw) == sha == result.result_sha256
    assert raw == (service.engine.work_root / str(rid) / "1/output/recommendation.json").read_bytes()
    api = client(service)
    response = api.get(f"/api/v1/rag/sessions/{sid}/casting-runs/{rid}/recommendation")
    assert response.status_code == 200 and response.content == raw
    assert response.headers["x-content-sha256"] == sha and response.headers["cache-control"] == "no-store"
    other = session(casting_db)
    assert api.get(f"/api/v1/rag/sessions/{other}/casting-runs/{rid}").status_code == 404
    assert api.get(f"/api/v1/rag/sessions/{other}/casting-runs/{rid}/recommendation").status_code == 404
    key = next(k for k in service.files.storage.objects if k[1].endswith("/output/recommendation.json"))
    service.files.storage.objects[key] = b"tampered"
    assert api.get(f"/api/v1/rag/sessions/{sid}/casting-runs/{rid}/recommendation").status_code == 503


@pytest.mark.casting_engine
@pytest.mark.parametrize("scenario,expected", [("no_candidate", "no_feasible_candidate"), ("admission", "admission_failed")])
def test_real_engine_business_outcomes(casting_db, stack, scenario, expected):
    service, sid = stack
    raw = (BACKEND / "tests/fixtures/casting/no_candidate.input.json").read_bytes()
    if scenario == "admission":
        data = json.loads(RAW)
        data["parameter_metadata"] = {"casting_mass_kg": {"status": "Proposed"}}
        raw = json.dumps(data).encode()
    file = service.files.upload(sid, uuid4(), "input.json", raw)
    turn = accept(casting_db, sid, file.file_id)
    result = service.execute_turn(sid, turn.id)
    assert result.status == expected
    if scenario == "no_candidate":
        data = json.loads(service.recommendation(sid, result.run_id)[0])
        assert data["candidates"] == [] and data["rejected_attempts"]
    else:
        assert result.error.category == "admission" and result.error.issues
        assert result.result_file_id is None and not service.files.list_inputs(sid).items[0].admission_passed


def test_partial_directory_is_interrupted_and_never_recomputed(casting_db, stack, monkeypatch):
    service, sid = stack
    file = service.files.upload(sid, uuid4(), "input.json", RAW)
    turn = accept(casting_db, sid, file.file_id)
    row = service.prepare(sid, turn.id)
    directory = service.engine.work_root / str(row.id) / "1"
    directory.mkdir(parents=True)
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("no blind recomputation"))
    assert service.execute_turn(sid, turn.id).status == "interrupted"


@pytest.mark.parametrize("options,code", [({"enabled": False}, "CASTING_FEATURE_DISABLED"),
    ({"wrapped": False}, "QA_LOCAL_TRANSPORT_REQUIRED"), ({"peer": "192.168.1.9"}, "QA_LOCAL_ACCESS_ONLY")])
def test_api_boundaries(stack, options, code):
    service, sid = stack
    response = client(service, **options).get(f"/api/v1/rag/sessions/{sid}/casting-inputs")
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("raw,filename,status,code", [(b"{", "in.json", 422, "CASTING_INPUT_INVALID"),
    (RAW, "in.txt", 422, "CASTING_FILE_TYPE"), (b"x" * (256 * 1024 + 1), "in.json", 413, "CASTING_FILE_TOO_LARGE"),
    (b"x" * (400 * 1024), "in.json", 413, "CASTING_FILE_TOO_LARGE")], ids=["parse", "extension", "file_budget", "request_budget"])
def test_api_upload_errors_are_bounded_and_safe(stack, raw, filename, status, code):
    service, sid = stack
    response = client(service).post(f"/api/v1/rag/sessions/{sid}/casting-inputs",
        data={"request_id": str(uuid4())}, files={"file": (filename, raw, "application/json")})
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert "object_key" not in response.text and "PRIVATE" not in response.text


def test_api_upload_list_and_paginate(stack):
    service, sid = stack
    api = client(service)
    path = f"/api/v1/rag/sessions/{sid}/casting-inputs"
    for _ in range(3):
        assert api.post(path, data={"request_id": str(uuid4())}, files={"file": ("input.json", RAW)}).status_code == 200
    page = api.get(path + "?limit=2").json()["data"]
    assert len(page["items"]) == 2 and page["next_before_id"]
    assert len(api.get(path + "?before_id=" + page["next_before_id"]).json()["data"]["items"]) == 1


@pytest.mark.casting_engine
def test_real_minio_original_bytes_and_complete_artifact_set(casting_db, stack):
    from app.core.config import Settings
    endpoint = os.environ.get("CASTING_TEST_MINIO_ENDPOINT", "")
    if not endpoint:
        pytest.skip("Requires an explicitly provisioned isolated MinIO endpoint")
    host, port = endpoint.split(":")
    assert host == "127.0.0.1" and 1024 < int(port) < 65536 and int(port) not in {9000, 9001}
    access = os.environ["CASTING_TEST_MINIO_ACCESS"]
    assert access.startswith("castingtest")
    storage = CastingObjectStorage(Settings(_env_file=None, minio_endpoint=endpoint,
        minio_root_user=access, minio_root_password=os.environ["CASTING_TEST_MINIO_SECRET"]))
    bucket = "casting-test-" + uuid4().hex
    service, sid = stack
    try:
        storage.client.make_bucket(bucket)  # Dedicated new test bucket, retained.
        service.files = CastingFiles(factory(casting_db), storage, bucket=bucket)
        file = service.files.upload(sid, uuid4(), "input.json", RAW)
        assert service.files.input_bytes(sid, file.file_id) == RAW
        turn = accept(casting_db, sid, file.file_id)
        result = service.execute_turn(sid, turn.id)
        raw, sha = service.recommendation(sid, result.run_id)
        assert digest(raw) == sha and result.status == "succeeded"
        with factory(casting_db)() as db:
            rows = list(db.scalars(select(CastingDesignFile).where(CastingDesignFile.session_id == sid)))
            names = {row.artifact_name for row in rows}
            assert {"rules.json", "engine-manifest.json", "input.json", "stdout.log", "stderr.log",
                    "output/recommendation.json", "output/knowledge-graph.owl", "output/shacl-report.json"} <= names
            for row in rows:
                assert row.storage_state == "ready" and row.object_key.startswith(f"casting/{sid}/")
                assert digest(service.files.read(row)) == row.sha256
        assert len(list(storage.client.list_objects(bucket, recursive=True))) == len(rows)
        # Real MinIO over-size reads must close the response and fail without
        # returning even a prefix as a valid result.
        row = rows[0]
        import copy
        short = copy.copy(row)
        short.size_bytes = 1
        with pytest.raises(CastingStorageError):
            service.files.read(short)
    finally:
        storage.close()


@pytest.mark.casting_engine
def test_actual_final_transaction_rollback_then_reuses_engine(casting_db, stack, monkeypatch):
    service, sid = stack
    file = service.files.upload(sid, uuid4(), "input.json", RAW)
    turn = accept(casting_db, sid, file.file_id)
    armed = [True]

    def fail_commit(db):
        if armed[0] and any(isinstance(row, CastingDesignRun) and row.session_id == sid
                           and row.status == "succeeded" for row in db.identity_map.values()):
            armed[0] = False
            raise RuntimeError("injected final SQL commit failure")

    event.listen(service.session_factory.class_, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="commit failure"):
            service.execute_turn(sid, turn.id)
    finally:
        event.remove(service.session_factory.class_, "before_commit", fail_commit)
    monkeypatch.setattr(service.engine, "execute", lambda *a, **kw: pytest.fail("calculation already finished"))
    assert service.execute_turn(sid, turn.id).status == "succeeded"


@pytest.mark.parametrize("code,status", [("CASTING_TIMEOUT", "timed_out"), ("CASTING_ENGINE_FAILED", "engine_failed")])
def test_execution_error_status_is_persisted(casting_db, stack, monkeypatch, code, status):
    service, sid = stack
    file = service.files.upload(sid, uuid4(), "input.json", RAW)
    turn = accept(casting_db, sid, file.file_id)
    def fail(*a, **kw):
        raise CastingExecutionError(code, "system" if code == "CASTING_TIMEOUT" else "engine", "受控故障")
    monkeypatch.setattr(service.engine, "execute", fail)
    result = service.execute_turn(sid, turn.id)
    assert result.status == status and result.error.code == code
    assert result.result_file_id is None and result.finished_at is not None


def test_run_creation_idempotency_and_frozen_rule_selection(casting_db, stack):
    service, sid = stack
    file = service.files.upload(sid, uuid4(), "input.json", RAW)
    turn = accept(casting_db, sid, file.file_id)
    with ThreadPoolExecutor(2) as executor:
        rows = list(executor.map(lambda _: service.prepare(sid, turn.id), range(2)))
    assert rows[0].id == rows[1].id and rows[0].call_key == rows[1].call_key
    row = rows[0]
    assert row.rule_sha256 == digest((ASSETS / "vendor/v5_1/rules-v1.json").read_bytes())
    assert row.engine_manifest_sha256 == digest((ASSETS / "engine-manifest.json").read_bytes())
    # Once DB identity is frozen, a replaced rule cannot fill a missing snapshot.
    from dataclasses import replace
    candidate = service.engine.rules.select(json.loads(RAW), "project-default")
    with pytest.raises(CastingStorageError) as caught:
        service._snapshot_rules(row, replace(candidate, raw=b"changed"))
    assert caught.value.code == "CASTING_SNAPSHOT_NOT_READY"


def test_enabled_application_lifespan_upload_and_ordinary_rag(casting_db, monkeypatch, tmp_path):
    from app.db import session as session_module
    from app.llm import provider as provider_module
    from app.main import create_app
    from app.services import casting_files as files_module
    from phase13_m3_support import ChatProvider, install_search, source

    provider = ChatProvider()
    storage = MemoryStorage()
    closed = []
    storage.close = lambda: closed.append(True)
    monkeypatch.setattr(session_module, "SessionLocal", factory(casting_db))
    monkeypatch.setattr(provider_module, "build_llm_provider", lambda _: provider)
    monkeypatch.setattr(files_module, "CastingObjectStorage", lambda _: storage)
    install_search(monkeypatch, [source(casting_db)])
    app = create_app(settings=settings_for(casting_db, conversation_enabled=True,
        casting_design_enabled=True, casting_work_root=tmp_path / "app-runs"))
    with TestClient(LocalConversationTransport(app), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 1234)) as api:
        response = api.post("/api/v1/rag/sessions", json={"request_id": str(uuid4())})
        sid = response.json()["data"]["thread_id"]
        path = f"/api/v1/rag/sessions/{sid}"
        uploaded = api.post(path + "/casting-inputs", data={"request_id": str(uuid4())}, files={"file": ("input.json", RAW)})
        assert uploaded.status_code == 200
        fid = uploaded.json()["data"]["file_id"]
        refused = api.post(path + "/turns", json={"request_id": str(uuid4()), "question": "计算浇冒系统", "casting_input_file_id": fid})
        assert refused.status_code == 503 and refused.json()["error"]["code"] == "CASTING_FEATURE_DISABLED"
        response = api.post(path + "/turns", json={"request_id": str(uuid4()), "question": "冒口有什么作用？"})
        assert response.status_code == 200 and response.json()["data"]["status"] == "completed"
        assert app.state.casting_design is not None
    assert app.state.casting_design is None and closed == [True] and provider.closed
