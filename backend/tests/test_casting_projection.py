"""Projection is a bounded copy of golden engine results, never a calculation."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from app.casting.execution_protocol import digest
from app.rag.casting_projection import RECOMMENDED_FIELDS, project_result, projection_bytes
from app.schemas.casting_storage import CastingErrorDetail, CastingStorageError

FIXTURES = Path(__file__).parent / "fixtures/casting"


class GoldenService:
    def __init__(self, data):
        self.raw = json.dumps(data, ensure_ascii=False).encode()
        self.run = NS(status="succeeded" if data["candidates"] else "no_feasible_candidate",
            input_file_id=uuid4(), result_file_id=uuid4(), result_sha256=digest(self.raw),
            rule_id=data["rule_set"], rule_version=data["rule_version"], rule_sha256="a" * 64,
            candidate_count=len(data["candidates"]), recommended_candidate_id=data["recommended_candidate_id"], error=None)
    def get_run(self, sid, rid):
        return self.run
    def recommendation(self, sid, rid):
        return self.raw, self.run.result_sha256


@pytest.mark.parametrize("fixture", ["baseline", "converted", "no_candidate"])
def test_projection_copies_golden_facts_and_reports_empty_candidates(fixture):
    data = json.loads((FIXTURES / f"{fixture}.recommendation.json").read_bytes())
    original = deepcopy(data)
    service = GoldenService(data)
    result = project_result(service, uuid4(), uuid4())
    assert result["candidate_count"] == len(data["candidates"])
    assert result["admission_conversions"] == data["admission"]["conversions"]
    assert sum(x["count"] for x in result["rejected_summary"]) == len(data["rejected_attempts"])
    assert len(projection_bytes(result)) < 24 * 1024
    if data["candidates"]:
        assert result["status"] == "success"
        assert result["recommended_candidate"] == {k: data["candidates"][0][k] for k in RECOMMENDED_FIELDS}
        result["recommended_candidate"]["risers"].clear()
    else:
        assert result["status"] == "no_feasible_candidate" and result["recommended_candidate"] is None
        assert result["rejected_summary"] and result["other_candidates"] == []
    assert data == original and json.loads(service.raw) == original


@pytest.mark.parametrize("corruption", ["bytes", "recommended", "count"])
def test_projection_rejects_integrity_disagreements(corruption):
    service = GoldenService(json.loads((FIXTURES / "baseline.recommendation.json").read_bytes()))
    if corruption == "bytes": service.raw += b" "
    if corruption == "recommended": service.run.recommended_candidate_id = "invented"
    if corruption == "count": service.run.candidate_count += 1
    with pytest.raises(CastingStorageError) as caught:
        project_result(service, uuid4(), uuid4())
    assert caught.value.code == "CASTING_OUTPUT_INVALID"


def test_projection_budget_does_not_silently_drop_recommended_facts():
    data = json.loads((FIXTURES / "baseline.recommendation.json").read_bytes())
    data["candidates"][0]["pending_evidence"] = ["x" * 25000]
    service = GoldenService(data)
    result = project_result(service, uuid4(), uuid4())
    assert result["summary_unavailable"] is True and result["projection"]["truncated"] is True
    assert result["recommended_candidate_id"] == data["recommended_candidate_id"]
    assert "recommended_candidate" not in result and len(projection_bytes(result)) < 24 * 1024


def test_error_projection_keeps_field_paths_and_does_not_load_recommendation():
    service = GoldenService(json.loads((FIXTURES / "no_candidate.recommendation.json").read_bytes()))
    service.run.status, service.run.result_file_id, service.run.result_sha256 = "admission_failed", None, None
    service.run.error = CastingErrorDetail(category="admission", code="CASTING_ADMISSION_FAILED",
        message="输入未准入", issues=[dict(field_path="casting_mass_kg", error_code="Missing", message="缺少参数")])
    service.recommendation = lambda *a: pytest.fail("failed runs have no recommendation")
    result = project_result(service, uuid4(), uuid4())
    assert result["status"] == "admission_failed" and result["result_file_id"] is None
    assert result["error"]["issues"][0]["field_path"] == "casting_mass_kg"


def test_large_admission_error_has_bounded_projection_and_retains_original_issues():
    from app.rag.casting_render import fact_catalog
    service = GoldenService(json.loads((FIXTURES / "no_candidate.recommendation.json").read_bytes()))
    service.run.status, service.run.result_file_id, service.run.result_sha256 = "admission_failed", None, None
    service.run.error = CastingErrorDetail(category="admission", code="CASTING_ADMISSION_FAILED", message="输入未准入",
        issues=[dict(field_path=f"hotspots.{n}", error_code="Invalid", message="错误" * 256) for n in range(100)])
    result = project_result(service, uuid4(), uuid4())
    assert len(projection_bytes(result)) < 24 * 1024 and result["projection"]["truncated"]
    assert result["error_issues_total"] == len(service.run.error.issues) == 100
    assert "其余字段问题见结构化错误详情" in fact_catalog(result)["error"]
