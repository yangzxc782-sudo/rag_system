"""Opt-in full browser + actual loopback HTTP + isolated PostgreSQL acceptance."""
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.rag.query_rewrite_prompt import current_rewrite_policy
from app.schemas.conversations import TurnCreateRequest
from app.services.conversations import Conversations
from phase13_m2_support import call, database_engine, publish, session, turn
from phase13_m3_support import install_search, redact, runtime, source
from phase13_m4_support import worker_target
from phase13_m5_support import browser_http_worker
from phase13_support import migration


pytestmark = pytest.mark.phase13_integration


def test_real_browser_over_m4_http(phase13_root_engine, monkeypatch, tmp_path):
    if os.environ.get("PHASE13_BROWSER_E2E") != "1":
        pytest.skip("Browser E2E requires PHASE13_BROWSER_E2E=1 and a verified isolated PG target")
    frontend = Path(__file__).resolve().parents[3] / "frontend"
    node = shutil.which("node")
    assert node and (frontend / ".next/BUILD_ID").is_file(), "Build the frontend with the synthetic API URL first"
    engine = database_engine(phase13_root_engine, revision="0008_phase10_enforce")
    with engine.begin() as db:
        migration(db, "0010_phase13_checkpoints")
    live, deleted, empty = source(engine), source(engine, n=2), source(engine, n=3)
    recovery_sid = session(engine)
    call(engine, lambda r: r.rename_session(recovery_sid, "中断恢复测试"))
    recovery_request = TurnCreateRequest(request_id=uuid4(), question=" 冒口尺寸如何确定？ ", limit=3, document_id=live["document_id"])
    call(engine, lambda r: r.start_turn(recovery_sid, recovery_request.request_id, recovery_request.question,
        limit=3, document_id=recovery_request.document_id, rewrite_policy=current_rewrite_policy()))
    deleted_sid = session(engine)
    call(engine, lambda r: r.rename_session(deleted_sid, "已删除来源测试"))
    with monkeypatch.context() as patches:
        install_search(patches, [deleted])
        graph, pool, _ = runtime(engine)
        service = Conversations(graph)
        try:
            service.submit(deleted_sid, TurnCreateRequest(request_id=uuid4(), question="冒口有什么作用？"))
        finally:
            service.close()
            pool.close()
    redact(engine, deleted["document_id"])
    history_sid = session(engine)
    call(engine, lambda r: r.rename_session(history_sid, "历史分页测试"))
    for i in range(32):
        publish(engine, turn(engine, history_sid, f"历史工艺问题 {i}"), content=f"历史已保存回答 {i}")
    manifest = tmp_path / "browser-manifest.json"
    journal = tmp_path / "browser-results.json"
    calls = tmp_path / "provider-calls.txt"
    manifest.write_text(json.dumps({"api": "http://127.0.0.1:18005", "recovery_thread": str(recovery_sid),
        "recovery_input": recovery_request.model_dump(mode="json"), "deleted_thread": str(deleted_sid),
        "history_thread": str(history_sid), "empty_document": empty["document_id"], "journal": str(journal),
        "verified_cluster": "phase13-m1-test-" + phase13_root_engine.url.database.removeprefix("phase13_m1_test_")}), encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    stop = ctx.Event()
    worker = ctx.Process(target=browser_http_worker, args=(worker_target(phase13_root_engine, engine), [live],
        empty["document_id"], 18005, "http://127.0.0.1:3306", str(calls), sender, stop))
    worker.start()
    try:
        assert receiver.poll(30) and receiver.recv() == 18005
        env = dict(os.environ, QA_E2E_MANIFEST=str(manifest), QA_E2E_PORT="3306")
        completed = subprocess.run([node, "node_modules/@playwright/test/cli.js", "test", "--project=isolated-postgresql", "--workers=1"],
            cwd=frontend, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=180)
        (tmp_path / "playwright.txt").write_text(completed.stdout, encoding="utf-8")
        assert completed.returncode == 0, completed.stdout
        report = json.loads(journal.read_text(encoding="utf-8"))
        with engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0010_phase13_checkpoints"
            for sid, count in report["messages"].items():
                assert db.scalar(text("SELECT count(*) FROM qa_messages WHERE session_id=:sid"), {"sid": sid}) == count
                assert db.scalar(text("SELECT count(*) FROM qa_turns WHERE session_id=:sid AND status <> 'completed'"), {"sid": sid}) == 0
            assert db.scalar(text("SELECT count(*) FROM langgraph_checkpoints.checkpoints")) > 0
            saved = db.execute(text("SELECT question,retrieval_limit,document_id,attempt_no FROM qa_turns WHERE session_id=:sid"), {"sid": recovery_sid}).one()
            assert tuple(saved) == (recovery_request.question, 3, recovery_request.document_id, 1)
            assert db.scalar(text("SELECT count(*) FROM qa_turn_artifacts WHERE kind='result'")) >= 10
        queries = calls.read_text(encoding="utf-8")
        assert "hybrid:冒口尺寸怎么确定？" in queries and "hybrid:冒口有哪些限制条件？" in queries
        assert "hybrid:铝合金热裂的原因是什么？" in queries
        print(completed.stdout)
        print("Real database messages / turns / attempt / checkpoints / artifacts / rewritten queries verified.")
    finally:
        stop.set()
        worker.join(15)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        receiver.close()
        sender.close()
        engine.dispose()  # Fixtures and isolated DB are deliberately retained.
