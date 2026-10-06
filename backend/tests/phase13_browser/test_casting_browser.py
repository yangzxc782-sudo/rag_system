"""No business URL fallback: guarded PostgreSQL + dedicated MinIO + real browser."""
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.services.casting_files import CastingObjectStorage
from casting_browser_support import casting_browser_worker
from phase13_m2_support import database_engine
from phase13_m3_support import source
from phase13_m4_support import worker_target

pytestmark = [pytest.mark.phase13_integration, pytest.mark.casting_engine]


def test_real_casting_browser(phase13_root_engine, tmp_path):
    if os.environ.get("CASTING_BROWSER_E2E") != "1":
        pytest.skip("Requires explicitly enabled isolated casting browser E2E")
    host, port = os.environ["CASTING_TEST_MINIO_ENDPOINT"].split(":")
    assert host == "127.0.0.1" and int(port) not in {9000, 9001}
    assert os.environ["CASTING_TEST_MINIO_ACCESS"].startswith("castingtest")
    frontend = Path(__file__).resolve().parents[3] / "frontend"
    node = shutil.which("node")
    assert node and (frontend / ".next/BUILD_ID").is_file()
    engine = database_engine(phase13_root_engine, revision="0012_casting_answers")
    storage = CastingObjectStorage(Settings(_env_file=None, minio_endpoint=os.environ["CASTING_TEST_MINIO_ENDPOINT"],
        minio_root_user=os.environ["CASTING_TEST_MINIO_ACCESS"], minio_root_password=os.environ["CASTING_TEST_MINIO_SECRET"]))
    bucket = "casting-test-" + uuid4().hex
    storage.client.make_bucket(bucket)  # New isolated bucket, always retained.
    storage.close()
    manifest, journal = tmp_path / "manifest.json", tmp_path / "browser-result.json"
    manifest.write_text(json.dumps({"api": "http://127.0.0.1:18005", "journal": str(journal),
        "verified_cluster": "phase13-m1-test-" + phase13_root_engine.url.database.removeprefix("phase13_m1_test_")}), encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    stop = ctx.Event()
    worker = ctx.Process(target=casting_browser_worker, args=(worker_target(phase13_root_engine, engine),
        [source(engine)], str(tmp_path), bucket, sender, stop))
    worker.start()
    try:
        assert receiver.poll(30) and receiver.recv() == 18005
        env = dict(os.environ, CASTING_E2E_MANIFEST=str(manifest), QA_E2E_PORT="3309",
            QA_E2E_OUTPUT_DIR=str(tmp_path / "playwright-results"), QA_E2E_REPORT=str(tmp_path / "playwright.xml"))
        completed = subprocess.run([node, "node_modules/@playwright/test/cli.js", "test", "--project=isolated-casting", "--workers=1"],
            cwd=frontend, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=150)
        (tmp_path / "playwright.txt").write_text(completed.stdout, encoding="utf-8")
        assert completed.returncode == 0, completed.stdout
        report = json.loads(journal.read_text(encoding="utf-8"))
        with engine.connect() as db:
            sid = report["thread_id"]
            assert db.scalar(text("SELECT count(*) FROM qa_messages WHERE session_id=:sid"), {"sid": sid}) == 10
            statuses = db.execute(text("SELECT status FROM casting_design_runs WHERE session_id=:sid ORDER BY created_at"), {"sid": sid}).scalars().all()
            assert statuses == ["succeeded", "no_feasible_candidate", "admission_failed"]
            assert db.scalar(text("SELECT count(*) FROM qa_turns WHERE session_id=:sid AND status <> 'completed'"), {"sid": sid}) == 0
            sha = db.scalar(text("SELECT result_sha256 FROM casting_design_runs WHERE id=:rid"), {"rid": report["run_id"]})
            assert sha == report["download_sha256"]
        assert (tmp_path / "engine-calls.txt").read_text().splitlines() == ["execute"] * 3
        print("Real browser upload / persisted filename / run reuse / no candidate / admission / RAG / download SHA verified.")
    finally:
        stop.set()
        worker.join(15)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        receiver.close(); sender.close(); engine.dispose()
