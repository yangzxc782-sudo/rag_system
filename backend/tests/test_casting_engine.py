"""Golden acceptance of the unmodified engine in a separate interpreter.

The small harness is test-only. It is not the future service/worker protocol.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest

BACKEND = Path(__file__).resolve().parents[1]
VENDOR = BACKEND / "app/casting/vendor/v5_1"
FIXTURES = Path(__file__).parent / "fixtures/casting"
pytestmark = pytest.mark.casting_engine

HARNESS = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from generate_design import run
from ontology_runtime import AdmissionError, metadata
if sys.argv[2] == 'metadata':
    print(json.dumps(metadata(), ensure_ascii=True))
else:
    try:
        run(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]),
            previous_dir=Path(sys.argv[5]) if sys.argv[5] else None,
            run_id=sys.argv[6] or None)
        print(json.dumps({'kind': 'success'}))
    except AdmissionError as exc:
        print(json.dumps({'kind': 'admission', 'report': exc.report}, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'kind': 'engine', 'type': type(exc).__name__, 'message': str(exc)}, ensure_ascii=True))
"""


@pytest.fixture(scope="module")
def engine_python():
    configured = os.environ.get("CASTING_TEST_PYTHON")
    default = BACKEND / ".venv-casting" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = Path(configured).resolve() if configured else default
    if not python.is_file():
        if configured:
            pytest.fail("CASTING_TEST_PYTHON does not point to an interpreter")
        pytest.skip("Create backend/.venv-casting from requirements-casting.txt or set CASTING_TEST_PYTHON")
    return python


def call_engine(python, input_path, outdir, *, rules=None, previous_dir=None, run_id=None):
    result = subprocess.run(
        [str(python), "-I", "-B", "-c", HARNESS, str(VENDOR), str(input_path),
         str(rules or VENDOR / "rules-v1.json"), str(outdir),
         str(previous_dir or ""), run_id or ""],
        cwd=outdir.parent, capture_output=True, text=True, encoding="utf-8", timeout=45,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalized(result):
    # RDF graph iteration order is undefined. Do not reorder candidates or checks.
    result["admission"]["rule_selection"].sort(key=lambda x: x["rule_id"])
    return result


@pytest.fixture(scope="module")
def baseline(engine_python, tmp_path_factory):
    directory = tmp_path_factory.mktemp("casting-baseline")
    assert call_engine(engine_python, VENDOR / "input-v1.json", directory) == {"kind": "success"}
    return directory


def test_engine_dependency_versions_are_frozen(engine_python):
    expected = dict(line.split("==") for line in (BACKEND / "requirements-casting.txt").read_text().splitlines()
                    if line and not line.startswith("#"))
    script = "import json, importlib.metadata as m, sys; print(json.dumps({n:m.version(n) for n in sys.argv[1:]}))"
    result = subprocess.run([str(engine_python), "-I", "-c", script, *expected], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


def test_baseline_matches_all_upstream_engineering_results(baseline):
    actual = normalized(read_json(baseline / "recommendation.json"))
    assert actual == read_json(FIXTURES / "baseline.recommendation.json")
    assert len(actual["candidates"]) == 4
    assert actual["recommended_candidate_id"] == "PMP-S1-C01"
    assert actual["candidates"][0]["yield_percent"] == 65.51
    assert read_json(baseline / "shacl-report.json")["conforms"] is True
    for filename in ("knowledge-graph.ttl", "knowledge-graph.owl", "shacl-report.ttl", "recommendation.md"):
        assert (baseline / filename).stat().st_size > 0


@pytest.mark.parametrize("scenario", ["converted", "no_candidate"])
def test_unit_conversion_and_no_candidate_golden(engine_python, tmp_path, scenario):
    input_path = FIXTURES / f"{scenario}.input.json"
    assert call_engine(engine_python, input_path, tmp_path / "output") == {"kind": "success"}
    result = normalized(read_json(tmp_path / "output/recommendation.json"))
    assert result == read_json(FIXTURES / f"{scenario}.recommendation.json")
    if scenario == "no_candidate":
        assert result["candidates"] == []
        assert result["recommended_candidate_id"] is None
        assert result["rejected_attempts"]
        assert read_json(tmp_path / "output/shacl-report.json")["conforms"] is True
    else:
        assert result["admission"]["conversions"][0]["value"] == 630.0
        assert read_json(input_path)["casting_mass_kg"] == 630000


@pytest.mark.parametrize("scenario,path,code", [
    ("missing", "input.casting_mass_kg", "Missing"),
    ("unconfirmed", "casting_mass_kg", "SHACL"),
    ("unit", "casting_mass_kg", "Unit"),
    ("scope", "rules.scope_material_family", "Excluded"),
])
def test_admission_report_is_structured_and_no_result_is_published(engine_python, tmp_path, scenario, path, code):
    data = read_json(VENDOR / "input-v1.json")
    if scenario == "missing":
        del data["casting_mass_kg"]
    elif scenario == "unconfirmed":
        data["parameter_metadata"] = {"casting_mass_kg": {"status": "Proposed"}}
    elif scenario == "unit":
        data["parameter_metadata"] = {"casting_mass_kg": {"unit": "litre"}}
    elif scenario == "scope":
        data["material_family"] = "OTHER-MATERIAL"
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(data), encoding="utf-8")
    outcome = call_engine(engine_python, input_path, tmp_path / "output")
    assert outcome["kind"] == "admission"
    assert outcome["report"]["conforms"] is False
    assert any(x["path"] == path and x["code"] == code and x["message"] for x in outcome["report"]["issues"])
    assert not (tmp_path / "output/recommendation.json").exists()
    # Upstream does not persist AdmissionError.report on failure; later worker must.
    assert not (tmp_path / "output/admission-report.json").exists()


@pytest.mark.parametrize("input_name,rules_name,count", [
    ("examples/input-v2.json", "examples/rules-v2.json", 3),
    ("examples/input-only-change.json", "rules-v1.json", 3),
    ("input-v1.json", "examples/rules-only-change.json", 4),
])
def test_packaged_examples_work_as_independent_runs(engine_python, tmp_path, input_name, rules_name, count):
    assert call_engine(engine_python, VENDOR / input_name, tmp_path / "output", rules=VENDOR / rules_name) == {"kind": "success"}
    assert len(read_json(tmp_path / "output/recommendation.json")["candidates"]) == count


def test_custom_run_id_and_separate_directories(engine_python, tmp_path, baseline):
    before = (baseline / "recommendation.json").read_bytes()
    assert call_engine(engine_python, VENDOR / "input-v1.json", tmp_path / "output", run_id="run-stage1") == {"kind": "success"}
    result = read_json(tmp_path / "output/recommendation.json")
    assert result["run_id"] == "run-stage1"
    assert result["recommended_candidate_id"] == "run-stage1-C01"
    assert result["candidates"][0]["yield_percent"] == 65.51
    assert (baseline / "recommendation.json").read_bytes() == before


def test_contract_metadata_paths_match_the_actual_ontology(engine_python, tmp_path):
    from app.schemas.casting_design import METADATA_PATH_PATTERN

    result = subprocess.run([str(engine_python), "-I", "-B", "-c", HARNESS, str(VENDOR), "metadata"],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    fields = json.loads(result.stdout)
    assert len(fields) == 19
    assert all(re.fullmatch(METADATA_PATH_PATTERN, field["path"].replace("*", "0")) for field in fields)


def test_rule_only_previous_graph_known_shacl_failure(engine_python, tmp_path, baseline):
    outcome = call_engine(engine_python, VENDOR / "input-v1.json", tmp_path / "output",
                          rules=VENDOR / "examples/rules-only-change.json", previous_dir=baseline)
    # This is an explicit regression capture of an upstream limitation, not a
    # blanket xfail that could hide a different exception or missing artifact.
    assert outcome["kind"] == "engine"
    assert outcome["type"] == "ValueError"
    report = read_json(tmp_path / "output/shacl-report.json")
    assert report["conforms"] is False
    assert len(report["issues"]) == 17
    assert all(x["message"].startswith("More than 1 values on ")
               and x["message"].endswith("->ex:numberValue")
               and x["code"] == "SHACL" for x in report["issues"])
    assert {"casting_mass_kg", "hotspots.0.modulus_mm"} <= {x["path"] for x in report["issues"]}
    assert not (tmp_path / "output/recommendation.json").exists()
