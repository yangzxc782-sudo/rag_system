"""M3 harness contracts only: these tests never import a real ML runtime."""
from dataclasses import replace
import importlib
import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from app.retrieval.reranker import RerankCandidate, RerankRequest, RerankResult, RerankScore


def harness():
    return importlib.import_module("tests.phase12_local.bge_smoke")


@pytest.mark.parametrize("value", [None, "", "false", "0", "true"])
def test_default_gate_has_zero_real_operations(tmp_path, value):
    operation = Mock(side_effect=AssertionError("real operation"))
    env = {} if value is None else {"PHASE12_BGE_PROBE_ENABLED": value}
    result = harness().run_smoke(tmp_path / "unused.json", environ=env, operation=operation)
    assert result == {"status": "disabled", "real_load_count": 0}
    operation.assert_not_called()
    assert not (tmp_path / "unused.json").exists()


def test_existing_gate_is_the_only_opt_in(tmp_path):
    operation = Mock(return_value={"status": "ok"})
    assert harness().run_smoke(tmp_path / "unused.json", environ={"PHASE12_BGE_PROBE_ENABLED": "1"},
                               operation=operation) == {"status": "ok"}
    operation.assert_called_once()


def test_import_and_disabled_cli_never_import_ml():
    code = '''
import importlib.abc, sys
class RejectML(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in ('torch', 'transformers', 'sentence_transformers'):
            raise AssertionError('ML import')
sys.meta_path.insert(0, RejectML())
from tests.phase12_local.bge_smoke import run_smoke
assert run_smoke('unused.json', environ={})['status'] == 'disabled'
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def gpu(allocated=100, reserved=200):
    return dict(allocated_bytes=allocated, reserved_bytes=reserved, allocated_peak_bytes=allocated,
                reserved_peak_bytes=reserved, device_total_bytes=1000, device_free_bytes=500,
                device_used_bytes=500)


def test_metric_recorder_percentiles_and_gpu_peaks():
    recorder = harness().MetricRecorder()
    for i in range(20):
        recorder.add(float(i), gpu(100, 200 + i))
    result = recorder.summary()
    assert result["p50_ms"] == 9.5 and result["p95_ms"] == pytest.approx(18.05)
    assert result["sample_count"] == 20 and result["fail_count"] == 0
    assert result["reserved_peak_bytes"] == 219 and result["device_snapshot_peak_bytes"] == 500


def test_metric_recorder_rejects_missing_gpu_and_insufficient_samples():
    recorder = harness().MetricRecorder()
    with pytest.raises(Exception, match="gpu_metric"):
        recorder.add(1.0, {})
    with pytest.raises(Exception, match="insufficient"):
        recorder.summary()


def request_result():
    request = RerankRequest("req", "PRIVATE QUERY", (RerankCandidate("a", 1, "PRIVATE BODY"),
                                                    RerankCandidate("b", 2, "PRIVATE BODY2")))
    return request, RerankResult("req", (RerankScore("b", 2, 7.0), RerankScore("a", 1, -2.0)))


@pytest.mark.parametrize("fault", ["request", "identity", "count", "nan", "partial"])
def test_score_identity_validation(fault):
    req, result = request_result()
    if fault == "request":
        result = replace(result, request_id="foreign")
    elif fault == "identity":
        result = replace(result, scores=(RerankScore("z", 2, 7.0), result.scores[1]))
    elif fault == "count":
        result = replace(result, scores=result.scores[:1])
    elif fault == "nan":
        result = replace(result, scores=(replace(result.scores[0], raw_score=float("nan")), result.scores[1]))
    else:
        result = replace(result, failure_reason="oom")
    with pytest.raises(harness().SmokeFailure):
        harness().score_record(req, result)


def test_score_record_retains_raw_scores_but_not_text():
    row = harness().score_record(*request_result())
    assert row == {"candidate_ids": ["a", "b"], "raw_scores": [-2.0, 7.0], "ranking": ["b", "a"]}
    assert "PRIVATE" not in json.dumps(row)


def test_fake_llm_uses_current_production_result_contract():
    from app.llm.provider import LLMGenerateRequest
    result = harness().SmokeLLM().generate(LLMGenerateRequest.from_prompt("fixture"))
    assert result.text == "Smoke fixture [1]" and result.model == "m3-fake"


def test_hybrid_fixture_respects_existing_public_uuid_schema():
    from app.schemas.search import SearchData
    from app.services.hybrid_search import HybridSearchResult
    from tests.phase12_local.bge_probe import load_cases
    cases = load_cases()[1][:8]
    sources = harness().smoke_hybrid_sources(cases)
    public = SearchData.model_validate(HybridSearchResult("fixture", 8, 8, sources))
    assert len({item.chunk_id for item in public.items}) == 8
    assert sources[0].content == cases[1].passage
    assert sources[1].content == cases[0].passage


def timeline():
    return dict(forward_start_ms=10, timeout_return_ms=52, busy_return_ms=53,
                forward_end_ms=120, idle_ms=121, recovery_ms=260, deadline_ms=50,
                busy_reason="busy", recovery_reason=None, max_active=1)


def test_real_timeout_busy_recovery_timeline():
    harness().validate_timeline(timeline())


@pytest.mark.parametrize("changes", [
    {"forward_start_ms": 60}, {"busy_return_ms": 130}, {"idle_ms": 100},
    {"timeout_return_ms": 151}, {"recovery_reason": "busy"}, {"max_active": 2},
    {"busy_reason": None},
])
def test_timeline_rejects_false_recovery_or_unbounded_wait(changes):
    with pytest.raises(harness().SmokeFailure):
        harness().validate_timeline({**timeline(), **changes})


@pytest.mark.parametrize("changes", [{"cancelled": False}, {"late_result": True},
    {"fallback_changed": True}, {"recovery_token": "old"}, {"recovery_request_matches": False}])
def test_late_result_validator(changes):
    evidence = dict(cancelled=True, late_result=False, fallback_changed=False,
                    old_token="old", recovery_token="new", recovery_request_matches=True)
    harness().validate_late_result(evidence)
    with pytest.raises(harness().SmokeFailure):
        harness().validate_late_result({**evidence, **changes})


def test_reserved_cache_is_allowed_but_allocated_growth_is_not():
    assert harness().validate_memory_trend([gpu(100, 200 + i) for i in range(20)])["allocated_growth_bytes"] == 0
    with pytest.raises(harness().SmokeFailure, match="memory_growth"):
        harness().validate_memory_trend([gpu(100 + i * 1024 * 1024, 100_000_000) for i in range(20)])


def test_report_redacts_content_credentials_and_paths():
    result = harness().safe_report({"query": "PRIVATE", "content": "BODY", "credential": "KEY",
        "nested": {"weights_path": "D:/secret", "raw_scores": [1.0]}}, sensitive=("PRIVATE", "BODY", "KEY"))
    assert result == {"nested": {"raw_scores": [1.0]}}
    for private in ("D:/weights", "/private/weights", "PRIVATE"):
        with pytest.raises(Exception):
            harness().safe_report({"diagnostic": private}, sensitive=("PRIVATE",))
