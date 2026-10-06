"""Phase two: real engine integration plus explicitly injected failure paths."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from app.casting.engine_worker import check_budget
from app.casting.execution_protocol import CastingExecutionError, ExecutionLimits, digest
from app.casting.process_control import calculation_slot, run_child
from app.services.casting_engine import CastingEngine, verify_completion
from app.services.casting_rules import ASSETS, CastingRuleSelector

BACKEND = Path(__file__).resolve().parents[1]
VENDOR = ASSETS / "vendor/v5_1"
FIXTURES = Path(__file__).parent / "fixtures/casting"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def raw_input():
    return (VENDOR / "input-v1.json").read_bytes()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(scope="module")
def engine_python():
    configured = os.environ.get("CASTING_TEST_PYTHON")
    python = Path(configured) if configured else BACKEND / ".venv-casting" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        if configured:
            pytest.fail("Invalid CASTING_TEST_PYTHON")
        pytest.skip("Isolated engine environment is required")
    return python.resolve()


@pytest.fixture(scope="module")
def completed(engine_python, tmp_path_factory):
    engine = CastingEngine(python_executable=engine_python, work_root=tmp_path_factory.mktemp("execution"))
    return engine.execute(raw_input())


@pytest.mark.casting_engine
def test_real_service_preserves_engineering_values_and_persists_audit(completed):
    expected = load(FIXTURES / "baseline.recommendation.json")
    actual = completed.recommendation
    for old, new in zip(expected["candidates"], actual["candidates"], strict=True):
        adjusted = deepcopy(old)
        adjusted["id"] = f"{completed.run_id}-C{old['rank']:02d}"
        adjusted["model_ref"] = f"cad://proposed/{completed.run_id}/candidate-{old['rank']:02d}"
        for explanation in adjusted["explanations"]:
            if "output_node" in explanation:
                explanation["output_node"] = explanation["output_node"].replace(old["id"], adjusted["id"])
        assert adjusted == new
    assert actual["recommended_candidate_id"] == completed.run_id + "-C01"
    audit = load(completed.directory / "execution.json")
    assert audit["status"] == "success"
    assert audit["input_sha256"] == digest(raw_input())
    assert audit["candidate_count"] == 4
    assert (completed.directory / "input.json").read_bytes() == raw_input()
    assert (completed.directory / "rules.json").read_bytes() == (VENDOR / "rules-v1.json").read_bytes()
    assert load(completed.directory / "runtime.json")["dependencies"] == audit["runtime_dependencies"]
    assert load(completed.directory / "capacity.json") == {"combinations": 63, "layouts": 1, "attempts": 4}


@pytest.mark.casting_engine
@pytest.mark.parametrize("scenario", ["converted", "no_candidate", "admission", "internal", "shacl", "after_result", "disk"])
def test_real_engine_and_injected_failure_classification(engine_python, tmp_path, monkeypatch, scenario):
    raw = raw_input()
    if scenario in ("converted", "no_candidate"):
        raw = (FIXTURES / f"{scenario}.input.json").read_bytes()
    elif scenario == "admission":
        data = json.loads(raw)
        data["parameter_metadata"] = {"casting_mass_kg": {"status": "Proposed"}}
        raw = json.dumps(data).encode()
    elif scenario in ("internal", "shacl", "after_result", "disk"):
        # Fault injection stays in this temporary wrapper; vendor bytes stay intact.
        helper = tmp_path / "injected.py"
        helper.write_text(f'''
import sys
from pathlib import Path
sys.path.insert(0, {str(BACKEND)!r})
sys.path.insert(0, {str(VENDOR)!r})
from app.casting import engine_worker
import generate_design
def fail(*args, **kwargs):
    if {scenario!r} == "shacl":
        from app.casting.execution_protocol import atomic_json
        atomic_json(Path(sys.argv[1]) / "output/shacl-report.json", {{"conforms": False, "issues": [{{"code": "SHACL"}}]}})
    if {scenario!r} == "disk":
        raise OSError("injected disk error with a private path")
    raise RuntimeError("injected internal detail")
if {scenario!r} == "internal": generate_design.size_risers = fail
elif {scenario!r} == "shacl": generate_design.export_graph = fail
else: generate_design.write_report = fail
raise SystemExit(engine_worker.main())
''', encoding="utf-8")
        monkeypatch.setattr("app.services.casting_engine.WORKER", helper)
    engine = CastingEngine(python_executable=engine_python, work_root=tmp_path / "runs")
    if scenario in ("converted", "no_candidate"):
        result = engine.execute(raw)
        assert result.status == ("success" if scenario == "converted" else "no_feasible_candidate")
        if scenario == "converted":
            assert result.recommendation["admission"]["conversions"][0]["value"] == 630.0
        else:
            assert result.recommendation["candidates"] == []
            assert result.recommendation["rejected_attempts"]
        return
    expected = {"admission": "CASTING_ADMISSION_FAILED", "internal": "CASTING_ENGINE_FAILED",
                "shacl": "CASTING_SHACL_FAILED", "after_result": "CASTING_ENGINE_FAILED", "disk": "CASTING_STORAGE_ERROR"}
    with pytest.raises(CastingExecutionError) as caught:
        engine.execute(raw)
    error = caught.value
    assert error.code == expected[scenario]
    assert load(error.run_directory / "execution.json")["status"] == "failed"
    assert "injected" not in json.dumps(error.public_dict())
    if scenario == "admission":
        report = load(error.run_directory / "admission-error.json")
        assert report["conforms"] is False
        assert any(x["field_path"] == "input.casting_mass_kg" and x["error_code"] == "SHACL" for x in error.issues)
    if scenario in ("after_result", "disk"):
        assert (error.run_directory / "output/recommendation.json").is_file()


def test_input_error_has_field_path_before_any_subprocess(tmp_path):
    engine = CastingEngine(python_executable=Path(sys.executable), work_root=tmp_path / "unused")
    with pytest.raises(CastingExecutionError) as caught:
        engine.execute(b'{"case_id":"a"}')
    assert caught.value.code == "CASTING_INPUT_INVALID"
    assert any(x["field_path"] == "input.casting_mass_kg" for x in caught.value.issues)
    assert not engine.work_root.exists()


@pytest.mark.parametrize("case", ["scope", "disabled", "ambiguous", "escape", "hash", "version", "capability", "engine", "malformed"])
def test_rule_selection_is_explicit_and_integrity_checked(tmp_path, case):
    assets = tmp_path / "assets"
    shutil.copytree(ASSETS, assets, ignore=shutil.ignore_patterns("__pycache__"))
    registry_path = assets / "rules/registry.json"
    registry = load(registry_path)
    entry = registry["entries"][0]
    data = json.loads(raw_input())
    expected = "CASTING_RULE_INTEGRITY"
    if case == "scope":
        data["material_family"] = "unknown"
        expected = "CASTING_RULE_NOT_APPLICABLE"
    elif case == "disabled":
        entry["enabled"] = False
        expected = "CASTING_RULE_NOT_APPLICABLE"
    elif case == "ambiguous":
        duplicate = deepcopy(entry)
        duplicate["key"] = "PMP-TRIAL-RULES@2"
        registry["entries"].append(duplicate)
        expected = "CASTING_RULE_AMBIGUOUS"
    elif case == "escape": entry["relative_path"] = "../engine-manifest.json"
    elif case == "hash": entry["sha256"] = "0" * 64
    elif case == "version": entry["rule_version"] = "2"
    elif case == "capability":
        path = assets / "engine-manifest.json"
        manifest = load(path)
        manifest["supported_scopes"] = []
        write_json(path, manifest)
        expected = "CASTING_ENGINE_UNSUPPORTED"
    elif case == "engine": (assets / "vendor/v5_1/shapes.ttl").write_text("changed")
    elif case == "malformed": registry["project_defaults"] = []
    write_json(registry_path, registry)
    with pytest.raises(CastingExecutionError) as caught:
        CastingRuleSelector(assets).select(data, "project-default")
    assert caught.value.code == expected


@pytest.mark.parametrize("case", ["combinations", "attempts", "catalog"])
def test_capacity_limits_do_not_truncate(case):
    data, rules = json.loads(raw_input()), load(VENDOR / "rules-v1.json")
    if case == "combinations":
        data["riser_sites"] = [dict(data["riser_sites"][0], id=f"RS-{i}") for i in range(24)]
    elif case == "attempts":
        for site in data["riser_sites"][:3]:
            data["riser_sites"].append(dict(site, id=site["id"] + "-ALT"))
    else:
        rules["riser_catalog"] *= 4
    with pytest.raises(CastingExecutionError) as caught:
        check_budget(data, rules, ExecutionLimits())
    assert caught.value.code == "CASTING_CAPACITY_EXCEEDED"


@pytest.mark.casting_engine
def test_over_capacity_worker_never_calculates(engine_python, tmp_path):
    engine = CastingEngine(python_executable=engine_python, work_root=tmp_path, limits=ExecutionLimits(max_attempts=3))
    with pytest.raises(CastingExecutionError) as caught:
        engine.execute(raw_input())
    assert caught.value.code == "CASTING_CAPACITY_EXCEEDED"
    assert not (caught.value.run_directory / "output").exists()


@pytest.mark.casting_engine
def test_directory_reuse_is_forbidden_and_new_execution_is_isolated(engine_python, tmp_path, completed):
    identity = uuid4()
    engine = CastingEngine(python_executable=engine_python, work_root=tmp_path)
    directory = tmp_path / str(identity) / "1"
    directory.mkdir(parents=True)
    (directory / "sentinel").write_bytes(b"keep")
    with pytest.raises(CastingExecutionError) as caught:
        engine.execute(raw_input(), run_id=identity)
    assert caught.value.code == "CASTING_RUN_CONFLICT"
    result = engine.execute(raw_input(), run_id=identity, execution_no=2)
    assert result.directory == tmp_path / str(identity) / "2"
    assert (directory / "sentinel").read_bytes() == b"keep"
    assert result.directory != completed.directory


@pytest.mark.parametrize("case", ["exit", "missing_marker", "identity", "hash", "shacl", "rank", "missing_file", "nan"])
def test_partial_or_tampered_result_never_publishes(tmp_path, completed, case):
    directory = tmp_path / "copy"
    shutil.copytree(completed.directory, directory)
    marker_path = directory / "worker-result.json"
    marker = load(marker_path)
    return_code = 0
    if case == "exit": return_code = 1
    elif case == "missing_marker": marker_path.unlink()
    elif case == "identity": marker["run_id"] = str(uuid4())
    elif case == "hash": marker["artifacts"]["recommendation.json"]["sha256"] = "0" * 64
    elif case == "missing_file": (directory / "output/recommendation.md").unlink()
    else:
        name = "shacl-report.json" if case == "shacl" else "recommendation.json"
        path = directory / "output" / name
        data = load(path)
        if case == "shacl": data["conforms"] = False
        elif case == "rank": data["candidates"][0]["rank"] = 2
        else: data["candidates"][0]["yield_percent"] = float("nan")
        write_json(path, data)
        marker["artifacts"][name] = dict(sha256=digest(path.read_bytes()), size_bytes=path.stat().st_size)
    if case != "missing_marker": write_json(marker_path, marker)
    with pytest.raises(CastingExecutionError) as caught:
        verify_completion(directory, load(directory / "request.json"), CastingRuleSelector().select(json.loads(raw_input()), "project-default"),
                          json.loads(raw_input()), return_code, ExecutionLimits())
    assert caught.value.code == "CASTING_OUTPUT_INVALID"


def test_slot_rejects_concurrency_and_releases_after_failure(tmp_path):
    with calculation_slot(tmp_path):
        with pytest.raises(CastingExecutionError) as caught:
            with calculation_slot(tmp_path):
                pytest.fail("Parallel calculation admitted")
        assert caught.value.code == "CASTING_BUSY"
    with pytest.raises(RuntimeError):
        with calculation_slot(tmp_path):
            raise RuntimeError("release test")
    with calculation_slot(tmp_path):
        pass


def test_slot_is_shared_by_separate_backend_processes(tmp_path):
    program = (
        f"import sys; sys.path.insert(0, {str(BACKEND)!r})\n"
        "from pathlib import Path\nfrom app.casting.process_control import calculation_slot\n"
        "from app.casting.execution_protocol import CastingExecutionError\n"
        "try:\n"
        f"    with calculation_slot(Path({str(tmp_path)!r})): print('unexpected')\n"
        "except CastingExecutionError as exc: print(exc.code)\n"
    )
    with calculation_slot(tmp_path):
        result = subprocess.run([sys.executable, "-I", "-B", "-c", program], capture_output=True, text=True,
                                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CASTING_BUSY"


@pytest.mark.casting_engine
def test_concurrent_service_call_is_busy_and_next_run_gets_fresh_directory(engine_python, tmp_path, monkeypatch):
    from threading import Event
    from app.services import casting_engine

    entered, release = Event(), Event()
    actual_run = casting_engine.run_child

    def paused(*args):
        entered.set()
        assert release.wait(timeout=10)
        return actual_run(*args)

    monkeypatch.setattr(casting_engine, "run_child", paused)
    engine = CastingEngine(python_executable=engine_python, work_root=tmp_path / "runs")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(engine.execute, raw_input())
        try:
            assert entered.wait(timeout=5)
            with pytest.raises(CastingExecutionError) as caught:
                CastingEngine(python_executable=engine_python, work_root=engine.work_root).execute(raw_input())
            assert caught.value.code == "CASTING_BUSY"
            assert caught.value.run_directory is None
        finally:
            release.set()
        first = future.result(timeout=30)
    second = engine.execute(raw_input())
    assert first.directory != second.directory
    assert load(first.directory / "execution.json")["status"] == "success"


@pytest.mark.casting_engine
def test_timeout_records_failure_and_releases_service_slot(engine_python, tmp_path):
    engine = CastingEngine(python_executable=engine_python, work_root=tmp_path, limits=ExecutionLimits(timeout_seconds=0.05))
    with pytest.raises(CastingExecutionError) as caught:
        engine.execute(raw_input())
    assert caught.value.code == "CASTING_TIMEOUT"
    assert load(caught.value.run_directory / "execution.json")["status"] == "failed"
    with calculation_slot(tmp_path):
        pass


@pytest.mark.casting_engine
def test_dependency_mismatch_is_a_system_error(engine_python, tmp_path):
    assets = tmp_path / "assets"
    shutil.copytree(ASSETS, assets, ignore=shutil.ignore_patterns("__pycache__"))
    path = assets / "engine-manifest.json"
    manifest = load(path)
    manifest["runtime_dependencies"]["rdflib"] = "0.0.0"
    write_json(path, manifest)
    engine = CastingEngine(python_executable=engine_python, work_root=tmp_path / "runs", rules=CastingRuleSelector(assets))
    with pytest.raises(CastingExecutionError) as caught:
        engine.execute(raw_input())
    assert caught.value.code == "CASTING_ENGINE_ENVIRONMENT"
    assert caught.value.category == "system"


def test_missing_interpreter_and_spawn_failure(tmp_path):
    engine = CastingEngine(python_executable=tmp_path / "missing.exe", work_root=tmp_path / "runs")
    with pytest.raises(CastingExecutionError) as caught:
        engine.execute(raw_input())
    assert caught.value.code == "CASTING_ENGINE_UNAVAILABLE"
    with pytest.raises(CastingExecutionError) as caught:
        run_child([str(tmp_path / "missing.exe")], tmp_path, ExecutionLimits())
    assert caught.value.code == "CASTING_ENGINE_UNAVAILABLE"


def helper_script(directory, content):
    path = directory / "helper.py"
    path.write_text("import sys, os, time, subprocess\nfrom pathlib import Path\n"
                    "if sys.stdin.buffer.read(1) != b'G': raise SystemExit(2)\n" + content, encoding="utf-8")
    return [sys.executable, "-I", "-B", str(path)]


@pytest.mark.parametrize("case,code", [("timeout", "CASTING_TIMEOUT"), ("log", "CASTING_LOG_LIMIT"), ("output", "CASTING_OUTPUT_LIMIT")])
def test_process_limits_and_reaping(tmp_path, case, code):
    commands = {
        "timeout": "Path('pid.txt').write_text(str(os.getpid()))\ntime.sleep(60)\n",
        "log": "sys.stdout.write('x'*65536)\nsys.stdout.flush()\ntime.sleep(60)\n",
        "output": "Path('large.bin').write_bytes(b'x'*65536)\ntime.sleep(60)\n",
    }
    command = helper_script(tmp_path, commands[case])
    limits = ExecutionLimits(timeout_seconds=0.5, max_log_bytes=1024,
                             max_output_bytes=32768, max_recommendation_bytes=1024)
    started = time.monotonic()
    with pytest.raises(CastingExecutionError) as caught:
        run_child(command, tmp_path, limits)
    assert caught.value.code == code
    assert time.monotonic() - started < 10
    assert (tmp_path / "stdout.log").stat().st_size <= 1024
    if case == "timeout":
        assert not process_alive(int((tmp_path / "pid.txt").read_text()))


def process_alive(pid):
    if os.name == "nt":
        import ctypes as c
        from ctypes import wintypes as w
        api = c.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)
        if not handle: return False
        try: return api.WaitForSingleObject(handle, 0) == 258
        finally: api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object parent-death acceptance")
def test_windows_parent_death_kills_worker_and_descendant(tmp_path):
    child_command = helper_script(tmp_path, "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], creationflags=subprocess.CREATE_NO_WINDOW)\n"
                                  "Path('pids.json').write_text(__import__('json').dumps([os.getpid(),child.pid]))\ntime.sleep(60)\n")
    parent = tmp_path / "parent.py"
    parent.write_text(f"import sys\nsys.path.insert(0,{str(BACKEND)!r})\nfrom pathlib import Path\n"
                      "from app.casting.process_control import run_child\nfrom app.casting.execution_protocol import ExecutionLimits\n"
                      f"run_child({child_command!r},Path({str(tmp_path)!r}),ExecutionLimits(timeout_seconds=60))\n", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-I", "-B", str(parent)], creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "pids.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pids = load(tmp_path / "pids.json")
        assert all(process_alive(pid) for pid in pids)
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while any(process_alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(process_alive(pid) for pid in pids)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
