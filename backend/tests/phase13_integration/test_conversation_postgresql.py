from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.core.errors import BusinessError
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.document_deletion_job import DocumentDeletionJob
from app.models.qa_evidence_snapshot import QAEvidenceSnapshot
from app.models.qa_evidence_source import QAEvidenceSource
from app.models.qa_message import QAMessage
from app.models.qa_session import QASession
from app.models.qa_turn import QATurn
from app.models.qa_turn_artifact import QATurnArtifact
from app.models.retrieval_log import RetrievalLog
from app.schemas.conversation_persistence import EvidenceSourceRef, SnapshotInput
from app.services.conversation_repository import ConversationError, ConversationRepository, fingerprint
from app.services.document_deletion import ClaimedDocumentDeletion, finalize_postgresql_deletion
from app.services.document_deletion_manifest import DocumentDeletionManifest
from app.services.document_qa_evidence_deletion import DocumentQaEvidenceDeletionService
from phase13_support import BASELINE, HEAD, migration, schema_engine


pytestmark = pytest.mark.phase13_integration


def call(engine, operation):
    # Match app.db.session.SessionLocal; repository writes must flush explicitly.
    with Session(engine, expire_on_commit=False, autoflush=False) as db, db.begin():
        return operation(ConversationRepository(db))


def new_session(engine):
    return call(engine, lambda r: r.create_session(uuid4(), title="M1 test")).id


def new_turn(engine, session_id=None, request_id=None):
    session_id = session_id or new_session(engine)
    turn = call(engine, lambda r: r.start_turn(session_id, request_id or uuid4(), "冒口有什么作用？"))
    return session_id, turn.id


def artifact(engine, session_id, turn_id, *, key="generation", kind="generation", attempt=1):
    return call(engine, lambda r: r.save_artifact(
        session_id, turn_id, expected_attempt=attempt, key=key, kind=kind, input_fingerprint=fingerprint({"input": 1}),
    )).id


def source(engine):
    with Session(engine) as db, db.begin():
        document_id, chunk_id = uuid4(), uuid4()
        db.add(Document(id=document_id, original_filename="synthetic.pdf", object_key=f"raw/2026/09/{document_id}.pdf",
                        bucket_name="phase13-no-storage", process_status="parsed", deletion_status="normal"))
        db.flush()
        db.add(DocumentChunk(id=chunk_id, document_id=document_id, chunk_index=0, content="SYNTHETIC_SOURCE_ONLY"))
        return EvidenceSourceRef(document_id, chunk_id)


def snapshots(engine, sid, tid, aid, *items, attempt=1):
    return call(engine, lambda r: r.save_snapshots(sid, tid, aid, expected_attempt=attempt, snapshots=tuple(items)))


def draft(engine, sid, tid, aid, refs=(), *, attempt=1):
    return snapshots(engine, sid, tid, aid, SnapshotInput(
        "answer", "answer_draft", {"text": "已保存的助手回答", "outcome": "answer" if refs else "clarification"}, refs,
    ), attempt=attempt)[0].id


def finalize(engine, sid, tid, snapshot_id):
    call(engine, lambda r: r.transition_turn(sid, tid, expected_status="running", new_status="finalizing", expected_attempt=1))
    return call(engine, lambda r: r.publish_answer(sid, tid, snapshot_id, expected_attempt=1))


def test_session_read_and_idempotent_creation(pg_engine):
    request_id = uuid4()
    first = call(pg_engine, lambda r: r.create_session(request_id, title="同一会话"))
    second = call(pg_engine, lambda r: r.create_session(request_id, title="同一会话"))
    assert first.id == second.id
    assert call(pg_engine, lambda r: r.get_session(first.id)).title == "同一会话"
    assert len(call(pg_engine, lambda r: r.list_sessions())) == 1
    with pytest.raises(ConversationError) as error:
        call(pg_engine, lambda r: r.create_session(request_id, title="不同参数"))
    assert error.value.code == "QA_REQUEST_CONFLICT"


def test_turn_idempotency_conflict_and_message_order(pg_engine):
    sid, request = new_session(pg_engine), uuid4()
    first = call(pg_engine, lambda r: r.start_turn(sid, request, "first", limit=8))
    again = call(pg_engine, lambda r: r.start_turn(sid, request, "first", limit=8))
    assert first.id == again.id
    for question, limit, document_id in [("changed", 8, None), ("first", 9, None), ("first", 8, uuid4())]:
        with pytest.raises(ConversationError) as error:
            call(pg_engine, lambda r: r.start_turn(sid, request, question, limit=limit, document_id=document_id))
        assert error.value.code == "QA_REQUEST_CONFLICT"
    aid = artifact(pg_engine, sid, first.id)
    answer_id = draft(pg_engine, sid, first.id, aid)
    answer = finalize(pg_engine, sid, first.id, answer_id)
    repeated = call(pg_engine, lambda r: r.publish_answer(sid, first.id, answer_id, expected_attempt=1))
    assert answer.id == repeated.id
    second = call(pg_engine, lambda r: r.start_turn(sid, uuid4(), "second"))
    messages = call(pg_engine, lambda r: r.list_messages(sid))
    assert [m.sequence_no for m in messages] == [1, 2, 3]
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    assert second.turn_no == 2
    assert [m.sequence_no for m in call(pg_engine, lambda r: r.list_messages(sid, limit=1, before_seq=3))] == [2]


def test_start_turn_rolls_back_insert_and_counters_after_midway_failure(pg_engine):
    sid = new_session(pg_engine)
    with Session(pg_engine) as db:
        def fail_user_insert(session, _context, _instances):
            if any(isinstance(row, QAMessage) for row in session.new):
                raise RuntimeError("injected user insert failure")
        event.listen(db, "before_flush", fail_user_insert)
        with pytest.raises(RuntimeError, match="injected"):
            with db.begin():
                ConversationRepository(db).start_turn(sid, uuid4(), "must roll back")
    with Session(pg_engine) as db:
        assert db.scalar(select(func.count()).select_from(QATurn)) == 0
        assert db.scalar(select(func.count()).select_from(QAMessage)) == 0
    session = call(pg_engine, lambda r: r.get_session(sid))
    assert (session.next_turn_no, session.next_message_seq) == (1, 1)
    turn = call(pg_engine, lambda r: r.start_turn(sid, uuid4(), "persist"))
    assert turn.turn_no == 1


def test_state_machine_attempt_fence_and_superseded_retry(pg_engine):
    sid, tid = new_turn(pg_engine)
    with pytest.raises(ConversationError, match="transition"):
        call(pg_engine, lambda r: r.transition_turn(sid, tid, expected_status="running", new_status="completed", expected_attempt=1))
    call(pg_engine, lambda r: r.transition_turn(sid, tid, expected_status="running", new_status="needs_recovery", expected_attempt=1, error_code="DB_UNAVAILABLE"))
    retried = call(pg_engine, lambda r: r.transition_turn(sid, tid, expected_status="needs_recovery", new_status="running", expected_attempt=1))
    assert retried.attempt_no == 2
    with pytest.raises(ConversationError) as error:
        artifact(pg_engine, sid, tid, attempt=1)
    assert error.value.code == "QA_ATTEMPT_CONFLICT"
    call(pg_engine, lambda r: r.transition_turn(sid, tid, expected_status="running", new_status="failed", expected_attempt=2, error_code="UPSTREAM_FAILED"))
    new_turn(pg_engine, sid)
    with pytest.raises(ConversationError) as error:
        call(pg_engine, lambda r: r.transition_turn(sid, tid, expected_status="failed", new_status="running", expected_attempt=2))
    assert error.value.code == "QA_TURN_SUPERSEDED"


def test_concurrent_same_request_creates_one_turn_and_one_message(pg_engine):
    sid, request, barrier = new_session(pg_engine), uuid4(), Barrier(4)
    def submit(_):
        barrier.wait(timeout=10)
        return call(pg_engine, lambda r: r.start_turn(sid, request, "concurrent")).id
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit, range(4)))
    assert len(set(ids)) == 1
    assert len(call(pg_engine, lambda r: r.list_messages(sid))) == 1
    assert call(pg_engine, lambda r: r.get_session(sid)).next_turn_no == 2


def test_concurrent_different_requests_only_accept_one_active_turn(pg_engine):
    sid, barrier = new_session(pg_engine), Barrier(2)
    def submit(_):
        barrier.wait(timeout=10)
        try:
            call(pg_engine, lambda r: r.start_turn(sid, uuid4(), "concurrent"))
            return "accepted"
        except ConversationError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(submit, range(2)))
    assert sorted(result) == ["QA_THREAD_BUSY", "accepted"]
    assert len(call(pg_engine, lambda r: r.list_messages(sid))) == 1


def test_concurrent_creation_and_publication_are_idempotent(pg_engine):
    request, barrier = uuid4(), Barrier(2)
    def create(_):
        barrier.wait(timeout=10)
        return call(pg_engine, lambda r: r.create_session(request)).id
    with ThreadPoolExecutor(max_workers=2) as pool:
        sessions = list(pool.map(create, range(2)))
    assert sessions[0] == sessions[1]
    sid, tid = new_turn(pg_engine, sessions[0])
    aid = artifact(pg_engine, sid, tid)
    snapshot_id = draft(pg_engine, sid, tid, aid)
    call(pg_engine, lambda r: r.transition_turn(sid, tid, expected_status="running", new_status="finalizing", expected_attempt=1))
    def publish(_):
        barrier.wait(timeout=10)
        return call(pg_engine, lambda r: r.publish_answer(sid, tid, snapshot_id, expected_attempt=1)).id
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(publish, range(2)))
    assert ids[0] == ids[1]
    assert len(call(pg_engine, lambda r: r.list_messages(sid))) == 2


@pytest.mark.parametrize("relation", ["message", "artifact", "snapshot", "source", "log"])
def test_database_rejects_cross_thread_associations(pg_engine, relation):
    s1, t1 = new_turn(pg_engine)
    s2, t2 = new_turn(pg_engine)
    a2 = artifact(pg_engine, s2, t2)
    snapshot2 = draft(pg_engine, s2, t2, a2)
    m2 = call(pg_engine, lambda r: r.list_messages(s2))[0].id
    rows = {
        "message": lambda: QAMessage(id=uuid4(), session_id=s1, turn_id=t2, role="assistant", sequence_no=2, content="bad", answer_snapshot_id=snapshot2),
        "artifact": lambda: QATurnArtifact(id=uuid4(), session_id=s1, turn_id=t1, parent_artifact_id=a2, attempt_no=1, artifact_key="bad", kind="result", input_fingerprint="0"*64, content_fingerprint="0"*64, details={}),
        "snapshot": lambda: QAEvidenceSnapshot(id=uuid4(), session_id=s1, turn_id=t1, artifact_id=a2, snapshot_key="bad", kind="graph", payload={}, content_fingerprint="0"*64),
        "source": lambda: QAEvidenceSource(id=uuid4(), session_id=s1, turn_id=t1, snapshot_id=snapshot2, document_id=uuid4()),
        "log": lambda: RetrievalLog(id=uuid4(), session_id=s1, turn_id=t1, message_id=m2, evidence_generation=1, query="bad", result_summary={}),
    }
    with pytest.raises(IntegrityError):
        with Session(pg_engine) as db, db.begin():
            db.add(rows[relation]())
            db.flush()
    with pytest.raises(ConversationError, match="this session"):
        call(pg_engine, lambda r: r.get_turn(s1, t2))
    with pytest.raises(ConversationError, match="this turn"):
        call(pg_engine, lambda r: r.get_artifact(s1, t1, a2))


@pytest.mark.parametrize("violation", ["request", "turn_no", "sequence", "role", "active", "bad_status"])
def test_database_constraints_without_repository(pg_engine, violation):
    sid, tid = new_turn(pg_engine)
    original = call(pg_engine, lambda r: r.get_turn(sid, tid))
    with pytest.raises(IntegrityError):
        with Session(pg_engine) as db, db.begin():
            if violation in {"sequence", "role"}:
                db.add(QAMessage(id=uuid4(), session_id=sid, turn_id=None if violation == "sequence" else tid,
                                 sequence_no=1 if violation == "sequence" else 2, role="user", content="bad"))
            else:
                db.add(QATurn(id=uuid4(), session_id=sid, request_id=original.request_id if violation == "request" else uuid4(),
                              turn_no=1 if violation == "turn_no" else 2, request_fingerprint="0"*64,
                              question="bad", retrieval_limit=8, status="running" if violation == "active" else "invalid" if violation == "bad_status" else "failed",
                              error_code=None if violation in {"active", "bad_status"} else "TEST_FAILED"))
            db.flush()


def test_artifact_snapshot_immutability_and_log_metadata(pg_engine):
    sid, tid = new_turn(pg_engine)
    aid = artifact(pg_engine, sid, tid)
    assert artifact(pg_engine, sid, tid) == aid
    with pytest.raises(ConversationError) as error:
        call(pg_engine, lambda r: r.save_artifact(sid, tid, expected_attempt=1, key="generation", kind="generation",
                                                input_fingerprint=fingerprint({"changed": True})))
    assert error.value.code == "QA_ARTIFACT_CONFLICT"
    ref = source(pg_engine)
    spec = SnapshotInput("citation:1", "citation", {"content": "EXCERPT_SENTINEL"}, (ref,))
    first = snapshots(pg_engine, sid, tid, aid, spec)[0]
    assert snapshots(pg_engine, sid, tid, aid, spec)[0].id == first.id
    assert call(pg_engine, lambda r: r.get_artifact_by_key(sid, tid, attempt_no=1, key="generation")).id == aid
    assert call(pg_engine, lambda r: r.get_artifact_by_key(sid, tid, attempt_no=2, key="generation")) is None
    assert [s.id for s in call(pg_engine, lambda r: r.list_snapshots(sid, tid, aid))] == [first.id]
    with pytest.raises(ConversationError) as error:
        snapshots(pg_engine, sid, tid, aid, SnapshotInput("citation:1", "citation", {"content": "changed"}, (ref,)))
    assert error.value.code == "QA_SNAPSHOT_CONFLICT"
    mid = call(pg_engine, lambda r: r.list_messages(sid))[0].id
    log = call(pg_engine, lambda r: r.record_retrieval(sid, tid, mid, expected_attempt=1, evidence_generation=1,
                                                    query="冒口", top_k=8, metrics={"candidate_count": 2}))
    replay = call(pg_engine, lambda r: r.record_retrieval(sid, tid, mid, expected_attempt=1, evidence_generation=1,
                                                       query="冒口", top_k=8, metrics={"candidate_count": 2}))
    assert log.id == replay.id
    with pytest.raises(ConversationError, match="typed persistence metrics"):
        call(pg_engine, lambda r: r.record_retrieval(sid, tid, mid, expected_attempt=1, evidence_generation=2,
                                                   query="冒口", top_k=8, metrics={"content": "EXCERPT_SENTINEL"}))


def test_source_validation_and_document_first_lock_guard(pg_engine):
    sid, tid = new_turn(pg_engine)
    aid = artifact(pg_engine, sid, tid)
    r1, r2 = source(pg_engine), source(pg_engine)
    with pytest.raises(ConversationError, match="another document"):
        snapshots(pg_engine, sid, tid, aid, SnapshotInput("bad", "citation", {}, (EvidenceSourceRef(r1.document_id, r2.chunk_id),)))
    with pytest.raises(ConversationError, match="normalized sources"):
        snapshots(pg_engine, sid, tid, aid, SnapshotInput("bad", "graph", {}))
    with pytest.raises(ConversationError) as error:
        with Session(pg_engine) as db, db.begin():
            repo = ConversationRepository(db)
            repo.save_artifact(sid, tid, expected_attempt=1, key="locks", kind="context", input_fingerprint="0"*64)
            repo.save_snapshots(sid, tid, aid, expected_attempt=1, snapshots=(SnapshotInput("bad", "graph", {}, (r1,)),))
    assert error.value.code == "QA_LOCK_ORDER"


def test_publication_rollback_and_source_visibility(pg_engine):
    sid, tid = new_turn(pg_engine)
    aid, ref = artifact(pg_engine, sid, tid), source(pg_engine)
    snap = draft(pg_engine, sid, tid, aid, (ref,))
    call(pg_engine, lambda r: r.transition_turn(sid, tid, expected_status="running", new_status="finalizing", expected_attempt=1))
    with pytest.raises(RuntimeError, match="after publish"):
        with Session(pg_engine) as db, db.begin():
            ConversationRepository(db).publish_answer(sid, tid, snap, expected_attempt=1)
            raise RuntimeError("after publish")
    assert call(pg_engine, lambda r: r.get_answer(sid, tid)) is None
    assert call(pg_engine, lambda r: r.get_turn(sid, tid)).status == "finalizing"
    with Session(pg_engine) as db, db.begin():
        db.get(Document, ref.document_id).deletion_status = "deleting"
    view = call(pg_engine, lambda r: r.get_snapshot(sid, tid, snap))
    assert view.payload is None and view.status == "source_unavailable"
    with pytest.raises(ConversationError, match="available answer draft"):
        call(pg_engine, lambda r: r.publish_answer(sid, tid, snap, expected_attempt=1))


def prepare_delete(engine, ref):
    job_id, token = uuid4(), uuid4()
    manifest = DocumentDeletionManifest(
        schema_version=1, document_id=ref.document_id, bucket_name="phase13-no-storage",
        raw_object_key=f"raw/2026/09/{ref.document_id}.pdf", derived_object_keys=(), derived_prefixes=(),
        parse_run_ids=(), block_ids=(), asset_ids=(), chunk_ids=(ref.chunk_id,),
        knowledge_source_relation_ids=(), knowledge_item_ids=(), search_index_name="phase13-unused",
        search_index_alias="phase13-unused",
    )
    now = datetime.now(timezone.utc)
    with Session(engine) as db, db.begin():
        db.get(Document, ref.document_id).deletion_status = "deleting"
        db.add(DocumentDeletionJob(id=job_id, document_id=ref.document_id, status="processing",
                                   current_step="finalize_postgresql", step_attempts=1, max_attempts=5,
                                   manifest=manifest.to_payload(), locked_by="phase13-test", lease_token=token,
                                   locked_at=now, lease_expires_at=now+timedelta(hours=1)))
    return ClaimedDocumentDeletion(job_id, ref.document_id, "finalize_postgresql", 1, 5, manifest.to_payload(), token), manifest


def evidence_set(engine):
    sid, tid = new_turn(engine)
    aid, r1, r2 = artifact(engine, sid, tid), source(engine), source(engine)
    views = snapshots(engine, sid, tid, aid,
                      SnapshotInput("c1", "citation", {"content": "DELETE_ME_SENTINEL"}, (r1,)),
                      SnapshotInput("c2", "citation", {"content": "KEEP_ME_SENTINEL"}, (r2,)),
                      SnapshotInput("g", "graph", {"entity": "MULTI_SOURCE_SENTINEL"}, (r1, r2)))
    answer = draft(engine, sid, tid, aid, (r1, r2))
    finalize(engine, sid, tid, answer)
    return sid, tid, r1, r2, views, answer


def test_real_finalizer_redacts_all_copies_and_preserves_published_history(pg_engine):
    sid, tid, removed, retained, views, answer = evidence_set(pg_engine)
    claimed, manifest = prepare_delete(pg_engine, removed)
    with Session(pg_engine, autoflush=False) as db, db.begin():
        finalize_postgresql_deletion(db, claimed=claimed, manifest=manifest)
    with Session(pg_engine) as db:
        assert db.get(Document, removed.document_id) is None
        assert db.get(DocumentChunk, removed.chunk_id) is None
        assert db.get(DocumentDeletionJob, claimed.job_id) is None
        assert db.get(Document, retained.document_id) is not None
        all_payloads = str(list(db.scalars(select(QAEvidenceSnapshot.payload)).all()))
        assert "DELETE_ME_SENTINEL" not in all_payloads
        assert "MULTI_SOURCE_SENTINEL" not in all_payloads
        assert "KEEP_ME_SENTINEL" in all_payloads
        assert all(s.status == "available" for s in db.scalars(select(QAEvidenceSource).where(QAEvidenceSource.document_id == retained.document_id)))
    messages = call(pg_engine, lambda r: r.list_messages(sid))
    assert [m.content for m in messages] == ["冒口有什么作用？", "已保存的助手回答"]
    for ident in (views[0].id, views[2].id, answer):
        view = call(pg_engine, lambda r: r.get_snapshot(sid, tid, ident))
        assert view.status == "source_deleted" and view.payload is None
    assert call(pg_engine, lambda r: r.publish_answer(sid, tid, answer, expected_attempt=1)).id == messages[1].id
    with Session(pg_engine) as db, db.begin():
        repeated = DocumentQaEvidenceDeletionService(db).redact(removed.document_id)
        assert len(repeated.snapshot_ids) == 3


def test_cleanup_failure_rolls_back_entire_postgresql_finalization(pg_engine):
    sid, tid, removed, _, views, _ = evidence_set(pg_engine)
    claimed, manifest = prepare_delete(pg_engine, removed)
    with Session(pg_engine, autoflush=False) as db:
        def fail_after_redaction(document_id):
            DocumentQaEvidenceDeletionService(db).redact(document_id)
            raise RuntimeError("injected cleanup failure")
        with pytest.raises(RuntimeError, match="cleanup failure"):
            with db.begin():
                finalize_postgresql_deletion(db, claimed=claimed, manifest=manifest, qa_evidence_cleanup=fail_after_redaction)
    with Session(pg_engine) as db:
        assert db.get(Document, removed.document_id) is not None
        assert db.get(DocumentChunk, removed.chunk_id) is not None
        assert db.get(DocumentDeletionJob, claimed.job_id) is not None
        snapshot = db.get(QAEvidenceSnapshot, views[0].id)
        assert snapshot.status == "available" and snapshot.payload == {"content": "DELETE_ME_SENTINEL"}
        assert all(s.status == "available" for s in db.scalars(select(QAEvidenceSource)))
    assert len(call(pg_engine, lambda r: r.list_messages(sid))) == 2


def test_live_schema_matches_m1_model_keys(pg_engine):
    inspector = inspect(pg_engine)
    for name in ("qa_sessions", "qa_messages", "retrieval_logs", "qa_turns", "qa_turn_artifacts", "qa_evidence_snapshots", "qa_evidence_sources"):
        table = Base.metadata.tables[name]
        columns = {c["name"]: c for c in inspector.get_columns(name)}
        assert set(columns) == set(table.columns.keys())
        assert all(columns[c.name]["nullable"] == c.nullable for c in table.columns)
        expected = {c.name for c in table.constraints if c.name}
        actual = {c["name"] for method in (inspector.get_foreign_keys, inspector.get_unique_constraints, inspector.get_check_constraints) for c in method(name)}
        assert expected <= actual
    with pg_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD


def test_legacy_nonempty_upgrade_backfill_and_unused_downgrade(phase13_root_engine):
    engine = schema_engine(phase13_root_engine, revision=BASELINE)
    sid, empty_sid = uuid4(), uuid4()
    ids = [UUID(int=3), UUID(int=1), UUID(int=2)]
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO qa_sessions(id,title) VALUES (:id,'legacy'),(:empty,'empty')"), {"id": sid, "empty": empty_sid})
        for ident in ids:
            connection.execute(text("INSERT INTO qa_messages(id,session_id,role,content,created_at) VALUES (:id,:sid,'custom_legacy_role',:body,'2026-01-01T00:00:00Z')"),
                               {"id": ident, "sid": sid, "body": f"legacy {ident}"})
        for summary in (None, "null", "{}"):
            connection.execute(text("INSERT INTO retrieval_logs(id,session_id,message_id,query,result_summary) VALUES (:id,:sid,:mid,'legacy',CAST(:summary AS jsonb))"),
                               {"id": uuid4(), "sid": sid, "mid": ids[0], "summary": summary})
    with engine.begin() as connection:
        migration(connection, HEAD)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, sequence_no, turn_id, role FROM qa_messages ORDER BY sequence_no")).all()
        assert [(row.id, row.sequence_no, row.turn_id) for row in rows] == [(UUID(int=i), i, None) for i in (1, 2, 3)]
        assert all(row.role == "custom_legacy_role" for row in rows)
        assert connection.scalar(text("SELECT next_message_seq FROM qa_sessions WHERE id=:id"), {"id": sid}) == 4
        assert connection.scalar(text("SELECT next_message_seq FROM qa_sessions WHERE id=:id"), {"id": empty_sid}) == 1
    with engine.begin() as connection:
        migration(connection, BASELINE, downgrade=True)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM qa_messages")) == 3
        assert connection.scalar(text("SELECT count(*) FROM retrieval_logs")) == 3
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == BASELINE
    engine.dispose()


@pytest.mark.parametrize("invalid", ["cross_session", "unattributed_summary"])
def test_unsafe_legacy_upgrade_refuses_and_rolls_back(phase13_root_engine, invalid):
    engine = schema_engine(phase13_root_engine, revision=BASELINE)
    s1, s2, mid, log_id = uuid4(), uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO qa_sessions(id) VALUES (:a),(:b)"), {"a": s1, "b": s2})
        connection.execute(text("INSERT INTO qa_messages(id,session_id,role,content) VALUES (:id,:sid,'user','legacy')"), {"id": mid, "sid": s1})
        connection.execute(text("INSERT INTO retrieval_logs(id,session_id,message_id,query,result_summary) VALUES (:id,:sid,:mid,'legacy',CAST(:summary AS jsonb))"),
                           {"id": log_id, "sid": s2 if invalid == "cross_session" else s1, "mid": mid,
                            "summary": '{"content":"unattributed"}' if invalid == "unattributed_summary" else "{}"})
    with pytest.raises(DBAPIError, match="Phase 13 preflight"):
        with engine.begin() as connection:
            migration(connection, HEAD)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == BASELINE
        assert connection.scalar(text("SELECT count(*) FROM retrieval_logs")) == 1
        assert "sequence_no" not in {c["name"] for c in inspect(connection).get_columns("qa_messages")}
    engine.dispose()


def test_used_schema_downgrade_is_refused_without_data_loss(pg_engine):
    sid, tid = new_turn(pg_engine)
    with pytest.raises(DBAPIError, match="downgrade refused"):
        with pg_engine.begin() as connection:
            migration(connection, BASELINE, downgrade=True)
    assert call(pg_engine, lambda r: r.get_turn(sid, tid)).status == "running"
    assert len(call(pg_engine, lambda r: r.list_messages(sid))) == 1


@pytest.mark.parametrize("constraint", ["request", "turn_no", "sequence", "role"])
def test_database_unique_constraints_survive_concurrent_raw_writes(pg_engine, constraint):
    sid, request, barrier = new_session(pg_engine), uuid4(), Barrier(2)
    tid = answer = None
    if constraint == "role":
        _, tid = new_turn(pg_engine, sid)
        answer = draft(pg_engine, sid, tid, artifact(pg_engine, sid, tid))

    def insert(index):
        try:
            with Session(pg_engine) as db, db.begin():
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
                if constraint in {"sequence", "role"}:
                    db.add(QAMessage(
                        id=uuid4(), session_id=sid, turn_id=tid, answer_snapshot_id=answer,
                        sequence_no=1 if constraint == "sequence" else index + 2,
                        role="user" if constraint == "sequence" else "assistant", content="raw concurrent",
                    ))
                else:
                    db.add(QATurn(
                        id=uuid4(), session_id=sid, request_id=request if constraint == "request" else uuid4(),
                        turn_no=1 if constraint == "turn_no" else index + 1, request_fingerprint="0" * 64,
                        question="raw concurrent", retrieval_limit=8, status="failed", error_code="TEST_FAILED",
                    ))
                barrier.wait(timeout=10)
                db.flush()
            return "saved"
        except IntegrityError as exc:
            assert exc.orig.sqlstate == "23505"
            return "unique_rejection"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(insert, range(2))) == ["saved", "unique_rejection"]


@pytest.mark.parametrize("operation", ["snapshot", "publish"])
def test_source_writer_rechecks_document_after_waiting_for_deletion(pg_engine, operation):
    sid, tid = new_turn(pg_engine)
    aid, ref = artifact(pg_engine, sid, tid), source(pg_engine)
    answer = draft(pg_engine, sid, tid, aid, (ref,)) if operation == "publish" else None
    if answer:
        call(pg_engine, lambda r: r.transition_turn(sid, tid, expected_status="running", new_status="finalizing", expected_attempt=1))
    attempted = Event()

    def write_after_deletion():
        with Session(pg_engine) as db:
            try:
                with db.begin():
                    connection = db.connection()
                    connection.execute(text("SET LOCAL lock_timeout = '5s'"))

                    def observe(_conn, _cursor, statement, _params, _context, _many):
                        if "FROM documents" in statement and "FOR UPDATE" in statement:
                            attempted.set()

                    event.listen(connection, "before_cursor_execute", observe)
                    repo = ConversationRepository(db)
                    if answer:
                        repo.publish_answer(sid, tid, answer, expected_attempt=1)
                    else:
                        repo.save_snapshots(sid, tid, aid, expected_attempt=1, snapshots=(
                            SnapshotInput("late", "citation", {"text": "must not persist"}, (ref,)),
                        ))
            except BusinessError as exc:
                return exc.code
        return "unexpectedly_saved"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(pg_engine) as db, db.begin():
            document = db.scalar(select(Document).where(Document.id == ref.document_id).with_for_update())
            document.deletion_status = "deleting"
            db.flush()
            future = pool.submit(write_after_deletion)
            assert attempted.wait(timeout=10)
        assert future.result(timeout=10) == "DOCUMENT_DELETION_IN_PROGRESS"
    assert call(pg_engine, lambda r: r.get_answer(sid, tid)) is None
    with Session(pg_engine) as db:
        assert db.scalar(select(func.count()).select_from(QAEvidenceSnapshot).where(QAEvidenceSnapshot.snapshot_key == "late")) == 0


def test_deletion_waits_for_writer_then_redacts_its_new_evidence(pg_engine):
    sid, tid = new_turn(pg_engine)
    aid, ref, attempted = artifact(pg_engine, sid, tid), source(pg_engine), Event()

    def remove_evidence():
        with Session(pg_engine) as db, db.begin():
            connection = db.connection()
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))

            def observe(_conn, _cursor, statement, _params, _context, _many):
                if "FROM documents" in statement and "FOR UPDATE" in statement:
                    attempted.set()

            event.listen(connection, "before_cursor_execute", observe)
            document = db.scalar(select(Document).where(Document.id == ref.document_id).with_for_update())
            document.deletion_status = "deleting"
            return DocumentQaEvidenceDeletionService(db).redact(ref.document_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(pg_engine) as db, db.begin():
            view = ConversationRepository(db).save_snapshots(sid, tid, aid, expected_attempt=1, snapshots=(
                SnapshotInput("last", "citation", {"text": "newly committed excerpt"}, (ref,)),
            ))[0]
            future = pool.submit(remove_evidence)
            assert attempted.wait(timeout=10)
        assert future.result(timeout=10).snapshot_ids == (view.id,)
    assert call(pg_engine, lambda r: r.get_snapshot(sid, tid, view.id)).payload is None
