from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text

from app.db.conversation_lock import ThreadExecutionLocks
from app.rag.query_rewrite_prompt import current_rewrite_policy
from phase13_m2_support import call, database_engine, session
from phase13_m3_support import ANSWER, install_search, runtime, source, redact
from app.schemas.conversations import SessionCreateRequest, SessionUpdateRequest, TurnCreateRequest
from app.services.conversations import Conversations
from app.services.conversation_repository import ConversationError
from phase13_m4_support import conversation_worker, http_worker, lock_worker, worker_target


pytestmark = pytest.mark.phase13_integration


@pytest.fixture(scope="module")
def m4_engine(phase13_root_engine):
    engine = database_engine(phase13_root_engine)
    yield engine
    engine.dispose()


@pytest.fixture
def api_chat(m4_engine, monkeypatch):
    search = install_search(monkeypatch, [source(m4_engine)])
    graph, pool, provider = runtime(m4_engine, conversation_enabled=True)
    service = Conversations(graph)
    yield service, provider, search
    service.close()
    pool.close()


def question(text="冒口有什么作用？", **kwargs):
    return TurnCreateRequest(request_id=uuid4(), question=text, **kwargs)


def new_session(service):
    return service.create_session(SessionCreateRequest(request_id=uuid4())).thread_id


def test_crud_and_idempotent_creation_and_cursor(m4_engine, api_chat):
    service, _, _ = api_chat
    req = SessionCreateRequest(request_id=uuid4())
    first = service.create_session(req)
    assert service.create_session(req).thread_id == first.thread_id
    renamed = service.rename_session(first.thread_id, SessionUpdateRequest(title="手动标题"))
    assert service.create_session(req).title == renamed.title
    for _ in range(3):
        new_session(service)
    page = service.list_sessions(limit=2)
    next_page = service.list_sessions(limit=2, cursor=page.next_cursor)
    assert len(page.items) == 2 and set(r.thread_id for r in page.items).isdisjoint(r.thread_id for r in next_page.items)
    assert service.session_detail(first.thread_id).title == "手动标题"
    service.submit(first.thread_id, question())
    assert service.session_detail(first.thread_id).title == "手动标题"


@pytest.mark.parametrize("followup,expected", [("那它尺寸怎么确定？", "冒口尺寸怎么确定？"),
    ("有哪些限制条件？", "冒口有哪些限制条件？"), ("铝合金热裂产生的原因是什么？", "铝合金热裂产生的原因是什么？")])
def test_full_turns_history_replay_and_parameters(api_chat, followup, expected):
    service, provider, search = api_chat
    sid = new_session(service)
    first = question(limit=1, document_id=UUID(search.sources[0]["document_id"]))
    status, answer = service.submit(sid, first)
    assert status == 200 and answer.status == "completed" and answer.result.answer == ANSWER
    before = len(provider.calls), len(search.calls)
    replay_status, replay = service.submit(sid, first)
    assert replay_status == 200 and replay == answer
    assert (len(provider.calls), len(search.calls)) == before
    second = question(followup, limit=2)
    _, result = service.submit(sid, second)
    assert result.status == "completed"
    assert search.calls[-1]["query"] == expected and search.calls[-1]["document_id"] is None
    newest = service.messages(sid, limit=2)
    earlier = service.messages(sid, limit=2, before_seq=newest.next_before_seq)
    assert [x.sequence_no for x in earlier.items + newest.items] == [1, 2, 3, 4]
    assert newest.items[-1].result == result.result
    assert service.request_status(sid, second.request_id).result == result.result


@pytest.mark.parametrize("changes", [{"question": "冷铁？"}, {"limit": 2}, {"document_id": uuid4()}])
def test_idempotency_conflict(api_chat, changes):
    service, provider, search = api_chat
    sid, req = new_session(service), question(limit=1)
    service.submit(sid, req)
    with pytest.raises(ConversationError, match="different parameters") as error:
        service.submit(sid, req.model_copy(update=changes))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(service.messages(sid).items) == 2 and len(search.calls) == 1


def test_clarification_and_fresh_graph_followup(m4_engine, api_chat):
    service, provider, search = api_chat
    sid = new_session(service)
    service.submit(sid, question("冒口和冷铁有什么区别？"))
    _, clarify = service.submit(sid, question("它的尺寸怎么确定？"))
    assert clarify.outcome == "clarification" and len(search.calls) == 1
    graph2, pool2, provider2 = runtime(m4_engine)
    service2 = Conversations(graph2)
    try:
        _, answer = service2.submit(sid, question("冒口"))
        assert answer.outcome == "answer" and "冒口" in search.calls[-1]["query"]
        assert "尺寸" in search.calls[-1]["query"]
        assert len(service2.messages(sid).items) == 6
    finally:
        service2.close()
        pool2.close()


def test_empty_and_thread_isolation(api_chat):
    service, provider, search = api_chat
    a, b = new_session(service), new_session(service)
    _, answer = service.submit(a, question())
    _, clarify = service.submit(b, question("那它的尺寸呢？"))
    assert clarify.outcome == "clarification" and len(search.calls) == 1
    with pytest.raises(ConversationError) as error:
        service.request_status(b, answer.request_id)
    assert error.value.status_code == 404
    search.sources = []
    _, empty = service.submit(a, question("冷铁有什么作用？"))
    assert empty.outcome == "no_context" and len(provider.answer_calls) == 1
    assert all(m.turn_id != answer.turn_id for m in service.messages(b).items)


def test_published_source_deletion_and_completed_replay(m4_engine, api_chat):
    service, provider, search = api_chat
    sid, req = new_session(service), question()
    _, answer = service.submit(sid, req)
    assert len(answer.result.citations) == 1
    redact(m4_engine, search.sources[0]["document_id"])
    _, replay = service.submit(sid, req)
    assert replay.result.answer == ANSWER and not replay.result.citations
    assert replay.result.sources[0].status == "source_deleted"
    assert service.messages(sid).items[-1].result == replay.result
    assert len(provider.answer_calls) == len(search.calls) == 1


def test_thread_lock_concurrency_and_status(api_chat):
    service, provider, search = api_chat
    sid, req = new_session(service), question()
    entered, release = Event(), Event()
    def slow(request):
        entered.set()
        assert release.wait(15)
        return ANSWER
    provider.answer = slow
    try:
        with ThreadPoolExecutor(2) as workers:
            pending = workers.submit(service.submit, sid, req)
            assert entered.wait(10)
            active = service.request_status(sid, req.request_id)
            assert active.status == "running" and active.execution_active and not active.can_retry
            status, same = service.submit(sid, req)
            assert status == 202 and same.turn_id == active.turn_id
            with pytest.raises(ConversationError) as error:
                service.submit(sid, question("冷铁有什么作用？"))
            assert error.value.code == "THREAD_BUSY"
            with pytest.raises(ConversationError) as error:
                service.submit(sid, req.model_copy(update={"limit": 2}))
            assert error.value.code == "IDEMPOTENCY_CONFLICT"
            assert len(service.messages(sid).items) == 1
            release.set()
            assert pending.result(timeout=10)[0] == 200
    finally:
        release.set()
    assert len(search.calls) == len(provider.answer_calls) == 1


def test_different_threads_execute_in_parallel(api_chat):
    service, provider, search = api_chat
    ids = [new_session(service) for _ in range(2)]
    barrier = Barrier(2)
    def slow(request):
        barrier.wait(timeout=10)
        return ANSWER
    provider.answer = slow
    with ThreadPoolExecutor(2) as workers:
        answers = list(workers.map(lambda sid: service.submit(sid, question()), ids))
    assert all(status == 200 for status, _ in answers)
    assert len(search.calls) == 2
    assert answers[0][1].turn_id != answers[1][1].turn_id


def test_multiprocess_advisory_lock(phase13_root_engine, m4_engine):
    ctx = multiprocessing.get_context("spawn")
    target = worker_target(phase13_root_engine, m4_engine)
    a, b = str(uuid4()), str(uuid4())
    processes, releases = [], []
    try:
        for sid, expected in [(a, True), (a, False), (b, True)]:
            parent, child = ctx.Pipe()
            release = ctx.Event()
            process = ctx.Process(target=lock_worker, args=(target, sid, child, release))
            processes.append(process)
            releases.append(release)
            process.start()
            assert parent.poll(15)
            acquired, pid = parent.recv()
            assert acquired is expected
    finally:
        for release in releases:
            release.set()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(5)
    assert all(p.exitcode == 0 for p in processes)


@pytest.mark.parametrize("window", ["after_user_commit", "artifact", "draft",
                                   "after_terminal_checkpoint", "after_publication_commit"])
def test_five_real_process_crash_windows(phase13_root_engine, m4_engine, api_chat, tmp_path, window):
    service, _, search = api_chat
    sid, req = new_session(service), question()
    target = worker_target(phase13_root_engine, m4_engine)
    calls = tmp_path / "external_calls.txt"
    ctx = multiprocessing.get_context("spawn")
    def run(crash_at):
        parent, child = ctx.Pipe()
        process = ctx.Process(target=conversation_worker, args=(target, str(sid), req.model_dump(mode="json"),
                            search.sources, crash_at, str(calls), child))
        process.start()
        try:
            assert parent.poll(20)
            message = parent.recv()
            process.join(10)
            assert process.exitcode == (73 if crash_at else 0)
            return message
        finally:
            if process.is_alive():
                process.kill()
                process.join(5)
    assert run(window) == "crashed:" + window
    before = service.messages(sid)
    assert len(before.items) == (2 if window == "after_publication_commit" else 1)
    status = service.request_status(sid, req.request_id)
    assert status.status == ("completed" if window == "after_publication_commit" else "needs_recovery")
    if status.status != "completed":
        assert status.can_retry
        with pytest.raises(ConversationError) as error:
            service.submit(sid, question("冷铁有什么作用？"))
        assert error.value.code == "QA_THREAD_BUSY"
    http_status, restored = run(None)  # New process, new Graph, new physical connections.
    assert http_status == 200 and restored["status"] == "completed"
    assert restored["turn_id"] == str(status.turn_id)
    row = call(m4_engine, lambda r: r.get_turn_by_request(sid, req.request_id))
    assert row.attempt_no == 1 and row.status == "completed"
    assert len(service.messages(sid).items) == 2
    assert calls.read_text().splitlines().count("hybrid") == 1
    assert calls.read_text().splitlines().count("llm") == 1
    assert calls.read_text().splitlines().count("rewrite") == 1
    checkpoint = service.graph.get_state(sid)
    assert checkpoint.values["terminal_status"] == "result_staged" and not checkpoint.next
    with m4_engine.connect() as db:
        keys = db.scalars(text("SELECT artifact_key FROM qa_turn_artifacts WHERE turn_id=:id"), {"id": row.id}).all()
        assert len(keys) == 6 and len(set(keys)) == 6
        assert {"query_rewrite_policy:v2", "query_rewrite:v2"}.issubset(keys)
        assert db.scalar(text("SELECT count(*) FROM retrieval_logs WHERE turn_id=:id"), {"id": row.id}) == 1


def test_publication_rollback_then_reuse_draft(m4_engine, api_chat):
    service, provider, search = api_chat
    sid, req = new_session(service), question()
    def fail(event, graph, lease):
        if event == "before_publication_commit":
            # Flushed but invisible to another connection until commit.
            assert call(m4_engine, lambda r: r.get_answer(sid, r.get_turn_by_request(sid, req.request_id).id)) is None
            raise RuntimeError("synthetic commit interruption")
    service.execution_hook = fail
    with pytest.raises(ConversationError) as error:
        service.submit(sid, req)
    assert error.value.code == "QA_PUBLICATION_INTERRUPTED"
    assert len(service.messages(sid).items) == 1
    row = call(m4_engine, lambda r: r.get_turn_by_request(sid, req.request_id))
    assert row.status == "needs_recovery" and row.attempt_no == 1
    assert service.graph.get_state(sid).values["terminal_status"] == "result_staged"
    service.execution_hook = None
    assert service.submit(sid, req)[0] == 200
    assert len(provider.answer_calls) == len(search.calls) == 1


@pytest.mark.parametrize("moment", ["during_generate", "after_terminal_checkpoint", "before_publish"])
def test_source_invalidated_before_publish_and_new_attempt(m4_engine, api_chat, moment):
    service, provider, search = api_chat
    sid, req = new_session(service), question()
    did = search.sources[0]["document_id"]
    if moment == "during_generate":
        def answer(request):
            redact(m4_engine, did)
            return ANSWER
        provider.answer = answer
    else:
        service.execution_hook = lambda event, graph, lease: redact(m4_engine, did) if event == moment else None
    with pytest.raises(ConversationError):
        service.submit(sid, req)
    row = call(m4_engine, lambda r: r.get_turn_by_request(sid, req.request_id))
    assert row.status == "failed" and row.attempt_no == 1
    assert len(service.messages(sid).items) == 1
    provider.answer, service.execution_hook = ANSWER, None
    _, result = service.submit(sid, req)
    assert result.outcome == "no_context" and not result.result.citations
    assert call(m4_engine, lambda r: r.get_turn(sid, row.id)).attempt_no == 2
    assert len(search.calls) == 2


def test_failed_old_turn_cannot_rewrite_history(m4_engine, api_chat):
    from app.core.errors import BusinessError
    service, provider, search = api_chat
    sid, req = new_session(service), question()
    provider.answer = BusinessError("LLM_TIMEOUT", "synthetic", status_code=504)
    with pytest.raises(ConversationError):
        service.submit(sid, req)
    assert service.request_status(sid, req.request_id).status == "failed"
    provider.answer = ANSWER
    service.submit(sid, question("冷铁有什么作用？"))
    assert not service.request_status(sid, req.request_id).can_retry
    with pytest.raises(ConversationError) as error:
        service.submit(sid, req)
    assert error.value.code == "QA_TURN_SUPERSEDED"


@pytest.mark.parametrize("advance_attempt", [False, True])
def test_lost_lock_connection_and_late_executor_are_fenced(m4_engine, api_chat, advance_attempt):
    from app.db.conversation_lock import ThreadLockLost
    service, provider, search = api_chat
    sid, req = new_session(service), question()
    entered, release = Event(), Event()
    leases = []
    service.execution_hook = lambda name, graph, lease: leases.append(lease) if name == "before_graph" else None
    def slow(request):
        entered.set()
        assert release.wait(20)
        return "UNSAFE_LATE_ANSWER"
    provider.answer = slow
    new_graph, pool, new_provider = runtime(m4_engine)
    replacement = Conversations(new_graph)
    try:
        with ThreadPoolExecutor(1) as workers:
            old = workers.submit(service.submit, sid, req)
            try:
                assert entered.wait(10)
                pid = leases[0].backend_pid
                with m4_engine.begin() as db:
                    identity = db.execute(text("SELECT datname, usename, state FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}).one()
                    assert identity == (m4_engine.url.database, "phase13_m1", "idle")
                    # Exact synthetic PID, already confirmed to be on this isolated DB.
                    assert db.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
                status = replacement.request_status(sid, req.request_id)
                assert status.status == "needs_recovery" and status.can_retry
                if advance_attempt:
                    with replacement.locks.acquire(sid) as lease, lease.session_factory() as db, db.begin():
                        from app.services.conversation_repository import ConversationRepository
                        repo = ConversationRepository(db)
                        repo.transition_turn(sid, status.turn_id, expected_status="needs_recovery",
                                             new_status="failed", expected_attempt=1, error_code="LLM_TIMEOUT")
                _, answer = replacement.submit(sid, req)
                assert answer.result.answer == ANSWER
                checkpoint = replacement.graph.get_state(sid).config["configurable"]["checkpoint_id"]
                release.set()
                with pytest.raises(ThreadLockLost):
                    old.result(timeout=10)
                row = call(m4_engine, lambda r: r.get_turn(sid, status.turn_id))
                assert row.status == "completed" and row.attempt_no == (2 if advance_attempt else 1)
                assert replacement.graph.get_state(sid).config["configurable"]["checkpoint_id"] == checkpoint
                assert len(replacement.messages(sid).items) == 2
                assert "UNSAFE_LATE_ANSWER" not in str(replacement.messages(sid))
                with m4_engine.connect() as db:
                    assert db.scalar(text("SELECT count(*) FROM qa_turn_artifacts WHERE turn_id=:id AND kind='generation'"),
                                     {"id": row.id}) == 1
            finally:
                release.set()
    finally:
        replacement.close()
        pool.close()


def test_published_graph_source_redaction_keeps_independent_citation(m4_engine, monkeypatch):
    from test_rag_graph_fusion import graph_service
    from phase13_m2_support import settings_for
    rows = [source(m4_engine, n=i) for i in (1, 2)]
    search = install_search(monkeypatch, rows)
    graph_service_fake, _ = graph_service(settings_for(graph_retrieval_enabled=True))
    graph, pool, provider = runtime(m4_engine, graph_retrieval=graph_service_fake,
                                   graph_retrieval_enabled=True, conversation_answer_graph_tokens=4096)
    service = Conversations(graph)
    try:
        sid, req = new_session(service), question()
        _, initial = service.submit(sid, req)
        assert initial.result.graph.evidence
        redact(m4_engine, rows[0]["document_id"])
        _, replay = service.submit(sid, req)
        assert replay.result.answer == ANSWER and not replay.result.graph.evidence
        assert len(replay.result.citations) == 1 and replay.result.citations[0].citation_id == 2
        assert any(s.kind == "graph" and s.status == "source_deleted" for s in replay.result.sources)
        assert len(search.calls) == len(provider.answer_calls) == 1
    finally:
        service.close()
        pool.close()


def test_real_http_api_and_process_restart(phase13_root_engine, m4_engine, api_chat):
    import httpx
    service, _, search = api_chat
    target = worker_target(phase13_root_engine, m4_engine)
    ctx = multiprocessing.get_context("spawn")
    def start():
        parent, child = ctx.Pipe()
        stop = ctx.Event()
        process = ctx.Process(target=http_worker, args=(target, search.sources, child, stop))
        process.start()
        assert parent.poll(20)
        port = parent.recv()
        assert port
        return process, stop, httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", trust_env=False, timeout=15)
    def end(process, stop, client):
        client.close()
        stop.set()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)
        assert process.exitcode == 0
    process, stop, client = start()
    try:
        request = {"request_id": str(uuid4())}
        created = client.post("/rag/sessions", json=request)
        assert created.status_code == 200 and created.json()["success"]
        sid = created.json()["data"]["thread_id"]
        assert client.post("/rag/sessions", json=request).json()["data"]["thread_id"] == sid
        assert client.patch(f"/rag/sessions/{sid}", json={"title": "HTTP验收"}).status_code == 200
        assert client.get(f"/rag/sessions/{sid}").json()["data"]["title"] == "HTTP验收"
        assert client.get("/rag/sessions", params={"limit": 1}).json()["data"]["next_cursor"]
        first = question("冒口和冷铁有什么区别？").model_dump(mode="json")
        result = client.post(f"/rag/sessions/{sid}/turns", json=first)
        assert result.status_code == 200 and result.json()["data"]["outcome"] == "answer"
        conflict = client.post(f"/rag/sessions/{sid}/turns", json={**first, "limit": 2})
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        clarify = client.post(f"/rag/sessions/{sid}/turns", json=question("它的尺寸怎么确定？").model_dump(mode="json"))
        assert clarify.json()["data"]["outcome"] == "clarification"
        assert client.get("/rag/sessions", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        assert client.get("/rag/sessions", headers={"Host": "public.example"}).status_code == 403
        assert client.get("/rag/sessions/not-a-uuid").status_code == 422
        assert client.get(f"/rag/sessions/{uuid4()}").status_code == 404
    finally:
        end(process, stop, client)
    process, stop, client = start()
    try:
        reply = client.post(f"/rag/sessions/{sid}/turns", json=question("冒口").model_dump(mode="json"))
        assert reply.status_code == 200 and reply.json()["data"]["outcome"] == "answer"
        status = client.get(reply.request.url.copy_with(path=reply.headers["location"]))
        assert status.status_code == 200 and status.json()["data"]["status"] == "completed"
        newest = client.get(f"/rag/sessions/{sid}/messages", params={"limit": 2}).json()["data"]
        earlier = client.get(f"/rag/sessions/{sid}/messages", params={"before_seq": newest["next_before_seq"]}).json()["data"]
        assert [m["sequence_no"] for m in earlier["items"] + newest["items"]] == list(range(1, 7))
        assert len(service.messages(UUID(sid)).items) == 6
    finally:
        end(process, stop, client)


def test_slow_pending_checkpoint_writes_finish_before_external_call(m4_engine, api_chat, monkeypatch):
    from langgraph.checkpoint.postgres import PostgresSaver
    from time import sleep
    from phase13_m2_support import invoke, turn
    service, provider, search = api_chat
    original = PostgresSaver.put_writes
    def slow_write(self, *args, **kwargs):
        # The business fence is already holding its short QA transaction here.
        sleep(0.05)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(PostgresSaver, "put_writes", slow_write)
    def no_business_transaction(request=None):
        assert m4_engine.pool.checkedout() == 0
        return ANSWER
    search.before = no_business_transaction
    provider.answer = no_business_transaction
    row = turn(m4_engine, session(m4_engine), "冒口有什么作用？")
    assert invoke(service.graph, row)["outcome"] == "answer"


def test_concurrent_session_create_and_legacy_history(m4_engine, api_chat):
    from app.models.qa_message import QAMessage
    from phase13_m2_support import factory
    service, _, _ = api_chat
    req = SessionCreateRequest(request_id=uuid4())
    with ThreadPoolExecutor(4) as workers:
        rows = list(workers.map(lambda _: service.create_session(req), range(4)))
    assert len({r.thread_id for r in rows}) == 1
    sid = rows[0].thread_id
    with factory(m4_engine)() as db, db.begin():
        db.add(QAMessage(session_id=sid, sequence_no=1, role="legacy_tool", content="legacy history"))
    history = service.messages(sid)
    assert history.items[0].role == "legacy_tool" and history.items[0].turn_id is None


@pytest.mark.parametrize("field", ["request_id", "attempt_no", "result_artifact_id", "outcome"])
def test_terminal_checkpoint_mismatch_cannot_publish(m4_engine, api_chat, field):
    service, provider, search = api_chat
    sid, req = new_session(service), question()
    def mutate_result_read(event, graph, lease):
        if event != "after_terminal_checkpoint":
            return
        original = graph.get_state
        def mismatch(thread_id):
            saved = original(thread_id)
            changed = dict(saved.values)
            changed[field] = 2 if field == "attempt_no" else ("no_context" if field == "outcome" else str(uuid4()))
            return saved._replace(values=changed)
        graph.get_state = mismatch
    service.execution_hook = mutate_result_read
    with pytest.raises(ConversationError) as error:
        service.submit(sid, req)
    assert error.value.code == "QA_CHECKPOINT_RESULT_MISMATCH"
    row = call(m4_engine, lambda r: r.get_turn_by_request(sid, req.request_id))
    assert row.status == "needs_recovery" and row.error_code == "QA_CHECKPOINT_RESULT_MISMATCH"
    assert call(m4_engine, lambda r: r.get_answer(sid, row.id)) is None
    assert not service.request_status(sid, req.request_id).can_retry
    service.execution_hook = None
    with pytest.raises(ConversationError) as error:
        service.submit(sid, req)
    assert error.value.code == "QA_RECOVERY_CONFLICT"
    assert len(search.calls) == len(provider.answer_calls) == 1


def test_timeout_explicit_retry_advances_attempt_and_keeps_one_user(m4_engine, api_chat):
    from app.core.errors import BusinessError
    service, provider, search = api_chat
    sid, req = new_session(service), question()
    provider.answer = BusinessError("LLM_TIMEOUT", "synthetic", status_code=504)
    with pytest.raises(ConversationError):
        service.submit(sid, req)
    status = service.request_status(sid, req.request_id)
    assert status.status == "failed" and status.can_retry
    provider.answer = ANSWER
    assert service.submit(sid, req)[0] == 200
    row = call(m4_engine, lambda r: r.get_turn_by_request(sid, req.request_id))
    assert row.attempt_no == 2 and len(service.messages(sid).items) == 2
    assert len(provider.answer_calls) == len(search.calls) == 2


def test_bound_connection_graph_and_short_transactions(m4_engine, monkeypatch):
    search = install_search(monkeypatch, [])
    graph, pool, provider = runtime(m4_engine)
    locks = ThreadExecutionLocks(graph.nodes.settings)
    sid = session(m4_engine)
    try:
        with locks.acquire(sid) as lease:
            assert lease is not None
            with lease.session_factory() as db, db.begin():
                from app.services.conversation_repository import ConversationRepository
                row = ConversationRepository(db).start_turn(sid, uuid4(), "冒口有什么作用？", rewrite_policy=current_rewrite_policy())
            assert not lease.connection.in_transaction()
            bound = graph.bind_execution(lease)
            original_rewrite = provider.rewrite
            def rewrite_without_transaction(request):
                assert not lease.connection.in_transaction()
                with m4_engine.connect() as probe:
                    assert probe.scalar(text("SELECT state FROM pg_stat_activity WHERE pid=:pid"),
                                        {"pid": lease.backend_pid}) == "idle"
                return original_rewrite(request)
            provider.rewrite = rewrite_without_transaction
            state = bound.invoke(sid, row.id, row.request_id, row.attempt_no)
            assert state["outcome"] == "no_context"
            staged = bound.get_result(sid, row.id, row.request_id, row.attempt_no)
            assert staged.checkpoint_complete
            assert not lease.connection.in_transaction()
            with m4_engine.connect() as db:
                assert db.scalar(text("SELECT state FROM pg_stat_activity WHERE pid=:pid"), {"pid": lease.backend_pid}) == "idle"
            with lease.session_factory() as db, db.begin():
                ConversationRepository(db).publish_answer(sid, row.id, staged.draft_snapshot_id,
                                                          expected_attempt=1, finalize=True)
        assert call(m4_engine, lambda r: r.get_turn(sid, row.id)).status == "completed"
        assert len(search.calls) == 1 and len(provider.rewrite_calls) == 1 and not provider.answer_calls
    finally:
        locks.close()
        pool.close()
