from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from phase12_local import bge_probe as probe


class FakeLogits:
    def __init__(self, shape, rows):
        self.shape = shape
        self.rows = rows

    def tolist(self):
        return self.rows


class FakeEncoding(dict):
    def __init__(self, ids, sequences):
        super().__init__(input_ids=ids, attention_mask=[[1] * len(row) for row in ids])
        self.sequences = sequences

    def sequence_ids(self, index):
        return self.sequences[index]


class FakeTokenizer:
    def __init__(self, *, damage_query=False):
        self.damage_query = damage_query
        self.calls = []

    def encode(self, text, **kwargs):
        assert kwargs == {"add_special_tokens": False, "truncation": False}
        return list(map(ord, text))

    def num_special_tokens_to_add(self, pair):
        assert pair is True
        return 3

    def __call__(self, queries, passages, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["truncation"] == "only_second"
        assert kwargs["return_tensors"] == "pt"
        ids, sequences = [], []
        for query, passage in zip(queries, passages, strict=True):
            q = list(map(ord, query))
            if self.damage_query:
                q = q[:-1]
            p = list(map(ord, passage))[: kwargs["max_length"] - len(q) - 3]
            ids.append([1] + q + [2] + p + [3])
            sequences.append([None] + [0] * len(q) + [None] + [1] * len(p) + [None])
        return FakeEncoding(ids, sequences)


def candidates():
    return (
        probe.Candidate("short-0", "QQ", "short", "short"),
        probe.Candidate("long-1", "QQ", "long-passage-" * 10, "paragraph"),
    )


@pytest.mark.parametrize("value", [None, "", "0", "false", "true"])
def test_gate_denies_real_operation_without_exact_enablement(value):
    calls = []
    environ = {} if value is None else {"PHASE12_BGE_PROBE_ENABLED": value}
    with pytest.raises(probe.ProbeDisabled):
        probe.run_gated(lambda: calls.append("load"), environ=environ)
    assert calls == []


def test_gate_can_invoke_fake_without_requiring_process_env(monkeypatch):
    monkeypatch.delenv("PHASE12_BGE_PROBE_ENABLED", raising=False)
    assert probe.run_gated(lambda: "fake", environ={"PHASE12_BGE_PROBE_ENABLED": "1"}) == "fake"


def test_import_does_not_import_model_or_cuda_runtime():
    tests_dir = str(Path(__file__).resolve().parent)
    code = "import sys; sys.path.insert(0, sys.argv[1]); from phase12_local import bge_probe; print(any(n in sys.modules for n in ['torch', 'transformers', 'sentence_transformers', 'huggingface_hub']))"
    result = subprocess.run([sys.executable, "-B", "-c", code, tests_dir], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False"


def test_raw_scalar_logits_preserve_sign_and_input_order():
    assert probe.validate_logits(FakeLogits((2, 1), [[-3.5], [2.0]]), ["a", "b"]) == [-3.5, 2.0]


@pytest.mark.parametrize("shape,rows", [((2,), [1, 2]), ((1, 2), [[1, 2]]), ((2, 2), [[1, 2], [3, 4]]), ((3, 1), [[1], [2], [3]])])
def test_logits_shape_is_not_flattened_to_fit(shape, rows):
    with pytest.raises(probe.ProbeContractError):
        probe.validate_logits(FakeLogits(shape, rows), ["a", "b"])


@pytest.mark.parametrize("rows", [[[1.0]], [[1.0], [2.0], [3.0]], [[True], [2.0]], [["1"], [2.0]], [[float("nan")], [2.0]], [[1.0], [float("inf")]], [[1.0], [-float("inf")]]])
def test_invalid_score_count_or_nonfinite_values_are_rejected(rows):
    with pytest.raises(probe.ProbeContractError):
        probe.validate_logits(FakeLogits((2, 1), rows), ["a", "b"])


@pytest.mark.parametrize("ids", [["a"], ["a", "a"], ["a", ""], ["a", 2]])
def test_candidate_identity_count_and_uniqueness(ids):
    with pytest.raises(probe.ProbeContractError):
        probe.validate_logits(FakeLogits((2, 1), [[1.0], [2.0]]), ids)


@pytest.mark.parametrize("count", [0, 1, 19])
def test_benchmark_rejects_insufficient_warm_samples(count):
    with pytest.raises(probe.ProbeContractError):
        probe.summarize_latencies([1.0] * count)


def test_p50_p95_use_documented_linear_interpolation():
    summary = probe.summarize_latencies(list(range(1, 21)), fail_count=2)
    assert summary == {"sample_count": 20, "p50_ms": 10.5, "p95_ms": pytest.approx(19.05), "fail_count": 2}


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf")])
def test_invalid_latency_is_rejected(bad):
    with pytest.raises(probe.ProbeContractError):
        probe.summarize_latencies([bad] * 20)


def gpu_metrics():
    return {"allocated_bytes": 10, "reserved_bytes": 20, "allocated_peak_bytes": 12,
            "reserved_peak_bytes": 24, "device_total_bytes": 100, "device_free_bytes": 60,
            "device_used_bytes": 40}


@pytest.mark.parametrize("missing", list(gpu_metrics()))
def test_gpu_metric_missing_is_not_silently_zero(missing):
    metrics = gpu_metrics()
    del metrics[missing]
    with pytest.raises(probe.ProbeContractError):
        probe.validate_gpu_metrics(metrics)


def test_valid_gpu_metrics_remain_exact():
    assert probe.validate_gpu_metrics(gpu_metrics()) == gpu_metrics()


def test_only_passage_is_temporarily_truncated():
    rows = candidates()
    before = deepcopy(rows)
    tokenizer = FakeTokenizer()
    _, stats = probe.prepare_pairs(tokenizer, rows, max_length=16)
    assert rows == before
    assert all(s["query_preserved"] for s in stats)
    assert stats[0]["truncation_rate"] == 0
    assert stats[1]["retained_passage_tokens"] == 11
    assert stats[1]["truncation_rate"] > 0
    assert tokenizer.calls[0]["truncation"] == "only_second"


def test_query_too_long_rejected_before_paired_tokenization():
    tokenizer = FakeTokenizer()
    with pytest.raises(probe.ProbeContractError, match="query_exceeds_budget"):
        probe.prepare_pairs(tokenizer, [probe.Candidate("q", "query-too-long", "passage", "short")], max_length=8)
    assert tokenizer.calls == []


def test_actual_query_truncation_is_detected():
    with pytest.raises(probe.ProbeContractError, match="query_changed"):
        probe.prepare_pairs(FakeTokenizer(damage_query=True), candidates(), max_length=16)


def test_report_removes_query_chunk_and_weight_paths():
    private = ["PRIVATE QUERY", "PRIVATE CHUNK", "D:/private/model-weights"]
    raw = {"model_id": "BAAI/bge-reranker-v2-m3", "sample_count": 20,
           "query": private[0], "chunk": private[1], "model_path": private[2],
           "nested": {"passage": private[1], "weights_path": private[2], "p95_ms": 12.0}}
    report = probe.sanitize_report(raw, sensitive=private)
    assert report == {"model_id": "BAAI/bge-reranker-v2-m3", "sample_count": 20, "nested": {"p95_ms": 12.0}}
    assert all(value not in json.dumps(report) for value in private)


def test_report_rejects_private_text_under_an_unexpected_key():
    with pytest.raises(probe.ProbeContractError, match="private_report_value"):
        probe.sanitize_report({"notes": "PRIVATE QUERY"}, sensitive=["PRIVATE QUERY"])


def test_stability_records_score_drift_and_ranking_changes():
    result = probe.score_stability([[1.0, 2.0], [2.5, 2.0]], ["a", "b"])
    assert result["max_score_drift"] == 1.5
    assert result["ranking_stable"] is False
    assert result["rankings"] == [["b", "a"], ["a", "b"]]


def test_ties_preserve_original_candidate_rank():
    result = probe.score_stability([[1.0, 1.0], [1.0, 1.0]], ["z", "a"])
    assert result["ranking_stable"] is True
    assert result["rankings"] == [["z", "a"], ["z", "a"]]


def test_candidate_matrix_simulates_one_query_with_varied_passages():
    sanity, pool = probe.load_cases()
    assert len(sanity) == 8
    assert len(pool) == 32
    assert len({c.query for c in pool}) == 1
    assert {c.kind for c in pool} == {"short", "paragraph", "table"}


@pytest.mark.parametrize("count,expected", [(8, [8]), (16, [16, 8]), (32, [32, 16, 8])])
def test_default_batch_matrix(count, expected):
    assert probe.batch_sizes_for(count, None) == expected


def test_explicit_microbatch_experiment_does_not_change_candidate_capacity():
    assert probe.batch_sizes_for(32, [8]) == [8]
    assert probe.batch_sizes_for(32, [16, 8]) == [16, 8]


def test_batch_filter_rejects_empty_experiment():
    with pytest.raises(probe.ProbeContractError, match="no_selected_batch"):
        probe.batch_sizes_for(8, [16])
