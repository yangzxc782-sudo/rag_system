"""Opt-in, sequential M0 experiment. No production provider or RAG integration.

Importing this module uses only the standard library. Real operations require
PHASE12_BGE_PROBE_ENABLED=1; the benchmark never downloads missing models.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time


MODEL_ID = "BAAI/bge-reranker-v2-m3"
MIN_SAMPLES = 20
BACKEND = Path(__file__).resolve().parents[2]
FIXTURE = BACKEND / "tests/fixtures/phase12/bge_probe_cases.json"
GPU_KEYS = ("allocated_bytes", "reserved_bytes", "allocated_peak_bytes",
            "reserved_peak_bytes", "device_total_bytes", "device_free_bytes", "device_used_bytes")


class ProbeDisabled(RuntimeError):
    pass


class ProbeContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    query: str
    passage: str
    kind: str


def run_gated(operation, *, environ=None):
    environment = os.environ if environ is None else environ
    if environment.get("PHASE12_BGE_PROBE_ENABLED") != "1":
        raise ProbeDisabled("probe_disabled")
    return operation()


def _identity(ids):
    if not ids or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ProbeContractError("invalid_candidate_identity")


def validate_logits(logits, candidate_ids):
    _identity(candidate_ids)
    if tuple(logits.shape) != (len(candidate_ids), 1):
        raise ProbeContractError("logits_shape_mismatch")
    rows = logits.tolist()
    if len(rows) != len(candidate_ids):
        raise ProbeContractError("score_count_mismatch")
    scores = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 1:
            raise ProbeContractError("score_shape_mismatch")
        value = row[0]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ProbeContractError("nonfinite_or_invalid_score")
        scores.append(float(value))
    return scores


def summarize_latencies(samples, *, fail_count=0):
    if len(samples) < MIN_SAMPLES:
        raise ProbeContractError("insufficient_benchmark_samples")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in samples):
        raise ProbeContractError("invalid_latency")
    ordered = sorted(samples)

    def percentile(p):
        index = (len(ordered) - 1) * p
        low, high = math.floor(index), math.ceil(index)
        return ordered[low] + (ordered[high] - ordered[low]) * (index - low)

    return {"sample_count": len(samples), "p50_ms": percentile(0.5),
            "p95_ms": percentile(0.95), "fail_count": fail_count}


def validate_gpu_metrics(metrics):
    for key in GPU_KEYS:
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProbeContractError("missing_or_invalid_gpu_metric")
    return {key: metrics[key] for key in GPU_KEYS}


def token_counts(tokenizer, candidates):
    return {c.candidate_id: (tokenizer.encode(c.query, add_special_tokens=False, truncation=False),
                            len(tokenizer.encode(c.passage, add_special_tokens=False, truncation=False)))
            for c in candidates}


def prepare_pairs(tokenizer, candidates, *, max_length, counts=None):
    _identity([c.candidate_id for c in candidates])
    counts = token_counts(tokenizer, candidates) if counts is None else counts
    overhead = tokenizer.num_special_tokens_to_add(pair=True)
    if any(len(counts[c.candidate_id][0]) + overhead >= max_length for c in candidates):
        raise ProbeContractError("query_exceeds_budget")
    encoded = tokenizer([c.query for c in candidates], [c.passage for c in candidates],
                        padding=True, truncation="only_second", max_length=max_length, return_tensors="pt")
    ids = encoded["input_ids"]
    rows = ids.tolist() if hasattr(ids, "tolist") else ids
    stats = []
    for index, candidate in enumerate(candidates):
        sequence = encoded.sequence_ids(index)
        query_ids = [token for token, part in zip(rows[index], sequence, strict=True) if part == 0]
        original_query, original_passage_count = counts[candidate.candidate_id]
        if query_ids != original_query:
            raise ProbeContractError("query_changed")
        retained = sum(part == 1 for part in sequence)
        if len(rows[index]) > max_length or retained > original_passage_count:
            raise ProbeContractError("token_budget_mismatch")
        stats.append({"candidate_id": candidate.candidate_id, "kind": candidate.kind,
                      "query_tokens": len(original_query), "query_preserved": True,
                      "original_passage_tokens": original_passage_count,
                      "retained_passage_tokens": retained,
                      "truncation_rate": 1 - retained / original_passage_count if original_passage_count else 0.0})
    return encoded, stats


def sanitize_report(data, *, sensitive=()):
    forbidden_keys = {"query", "passage", "chunk", "content", "model_path", "weights_path",
                      "cache_dir", "traceback", "exception_message", "credential"}

    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in forbidden_keys}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        if isinstance(value, str) and re.search(r"[A-Za-z]:[\\/]", value):
            raise ProbeContractError("private_report_value")
        return value

    result = clean(data)
    serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if any(text and text in serialized for text in sensitive):
        raise ProbeContractError("private_report_value")
    return result


def score_stability(runs, candidate_ids):
    _identity(candidate_ids)
    if not runs or any(len(row) != len(candidate_ids) for row in runs):
        raise ProbeContractError("score_count_mismatch")
    if any(not math.isfinite(v) for row in runs for v in row):
        raise ProbeContractError("nonfinite_or_invalid_score")
    rankings = [[candidate_ids[i] for i in sorted(range(len(row)), key=lambda i: (-row[i], i, candidate_ids[i]))]
                for row in runs]
    return {"max_score_drift": max(abs(v - runs[0][i]) for row in runs for i, v in enumerate(row)),
            "ranking_stable": all(rank == rankings[0] for rank in rankings), "rankings": rankings}


def _sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def batch_sizes_for(count, selected):
    defaults = [count] if count == 8 else ([count, 8] if count == 16 else [count, 16, 8])
    batches = defaults if selected is None else [n for n in defaults if n in selected]
    if not batches:
        raise ProbeContractError("no_selected_batch")
    return batches


def _settings():
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from app.core.config import Settings
    return Settings()


def load_cases():
    spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    short = []
    for case in spec["sanity_cases"]:
        for label in ("relevant", "irrelevant"):
            short.append(Candidate(case["id"] + "-" + label, case["query"], case[label], "short"))
    pool = []
    for index in range(32):
        base = short[index % len(short)]
        kind = ("short", "paragraph", "table")[index % 3]
        passage = base.passage
        if kind == "paragraph":
            passage += "\n" + spec["long_paragraph_unit"] * spec["paragraph_repeats"]
        elif kind == "table":
            passage += "\n" + spec["table_header"] + "\n" + "\n".join(
                spec["table_row_template"].format(row=n) for n in range(spec["table_rows"]))
        pool.append(Candidate(f"candidate-{index:02d}", short[0].query, passage, kind))
    return short, pool


def _write(path, data, *, sensitive=()):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(sanitize_report(data, sensitive=sensitive), ensure_ascii=False,
                                 allow_nan=False, indent=2) + "\n", encoding="utf-8")


def download_model(output):
    def download():
        from huggingface_hub import HfApi, snapshot_download
        local = Path(_settings().reranker_model_path).resolve()
        if subprocess.run(["git", "check-ignore", "--quiet", "--", str(local / "model.safetensors")],
                          cwd=BACKEND.parent, check=False).returncode != 0:
            raise ProbeContractError("model_directory_not_ignored")
        api = HfApi(token=False)
        info = api.model_info(MODEL_ID, files_metadata=True)
        files = {f.rfilename for f in info.siblings}
        wanted = {"config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
                  "sentencepiece.bpe.model", "model.safetensors"} & files
        if not {"config.json", "tokenizer.json", "tokenizer_config.json", "model.safetensors"} <= wanted:
            raise ProbeContractError("official_model_assets_differ")
        snapshot_download(MODEL_ID, revision=info.sha, local_dir=str(local),
                          allow_patterns=sorted(wanted), token=False, max_workers=2)
        manifest = {"model_id": MODEL_ID, "revision": info.sha,
                    "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
                    "files": [{"name": name, "size_bytes": (local / name).stat().st_size,
                               "sha256": _sha(local / name)} for name in sorted(wanted)]}
        _write(output, manifest)
        print(json.dumps({"status": "downloaded", "model_id": MODEL_ID, "revision": info.sha,
                          "file_count": len(wanted)}), flush=True)
    return run_gated(download)


def gpu_snapshot(torch):
    free, total = torch.cuda.mem_get_info(0)
    return validate_gpu_metrics({"allocated_bytes": torch.cuda.memory_allocated(0),
        "reserved_bytes": torch.cuda.memory_reserved(0),
        "allocated_peak_bytes": torch.cuda.max_memory_allocated(0),
        "reserved_peak_bytes": torch.cuda.max_memory_reserved(0),
        "device_total_bytes": total, "device_free_bytes": free, "device_used_bytes": total - free})


def _runtime_evidence(torch):
    return {"python": platform.python_version(),
        "versions": {p: importlib.metadata.version(p) for p in
                     ("torch", "transformers", "sentence-transformers", "tokenizers", "safetensors", "huggingface-hub")},
        "cuda_build": torch.version.cuda, "gpu": torch.cuda.get_device_name(0),
        "capability": list(torch.cuda.get_device_capability(0)), "bf16_supported": torch.cuda.is_bf16_supported(),
        "torch_float32_matmul_precision": torch.get_float32_matmul_precision(),
        "tf32_matmul_allowed": torch.backends.cuda.matmul.allow_tf32,
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BACKEND, text=True).strip(),
        "probe_sha256": _sha(__file__), "fixture_sha256": _sha(FIXTURE)}


def _score_once(torch, tokenizer, model, candidates, max_length, batch_size, counts):
    torch.cuda.synchronize()
    started = time.perf_counter()
    scores, stats, shapes, snapshots = [], [], [], []
    forward_ms = 0.0
    with torch.inference_mode():
        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset:offset + batch_size]
            encoded, batch_stats = prepare_pairs(tokenizer, batch, max_length=max_length, counts=counts)
            inputs = {key: value.to("cuda:0") for key, value in encoded.items()}
            torch.cuda.synchronize()
            event_start, event_end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
            event_start.record()
            logits = model(**inputs, return_dict=True).logits
            event_end.record()
            torch.cuda.synchronize()
            forward_ms += event_start.elapsed_time(event_end)
            shapes.append(list(logits.shape))
            scores.extend(validate_logits(logits.detach().float().cpu(), [c.candidate_id for c in batch]))
            stats.extend(batch_stats)
            snapshots.append(gpu_snapshot(torch))
            del inputs, logits, encoded
    torch.cuda.synchronize()
    wall_ms = (time.perf_counter() - started) * 1000
    if len(scores) != len(candidates):
        raise ProbeContractError("score_count_mismatch")
    return {"scores": scores, "wall_ms": wall_ms, "forward_ms": forward_ms,
            "token_stats": stats, "logits_shapes": shapes, "gpu_samples": snapshots}


def _benchmark_cell(torch, tokenizer, model, candidates, max_length, batch_size, counts, samples, warmups):
    torch.cuda.empty_cache()  # Cached blocks only; live model and Embedding tensors remain resident.
    torch.cuda.reset_peak_memory_stats()
    before = gpu_snapshot(torch)
    config = {"candidate_count": len(candidates), "batch_size": batch_size, "max_length": max_length}
    runs = []
    try:
        for _ in range(warmups):
            _score_once(torch, tokenizer, model, candidates, max_length, batch_size, counts)
        for _ in range(samples):
            runs.append(_score_once(torch, tokenizer, model, candidates, max_length, batch_size, counts))
    except torch.cuda.OutOfMemoryError:
        return {**config, "status": "oom", "oom": True, "fail_count": 1,
                "sample_count": len(runs), "gpu_before": before, "gpu_after": gpu_snapshot(torch)}
    ids = [c.candidate_id for c in candidates]
    all_snapshots = [before] + [snap for row in runs for snap in row["gpu_samples"]]
    return {**config, "status": "ok", "oom": False, "load_success": True, "forward_success": True,
            "warm_wall": summarize_latencies([r["wall_ms"] for r in runs]),
            "warm_forward": summarize_latencies([r["forward_ms"] for r in runs]),
            "wall_samples_ms": [r["wall_ms"] for r in runs],
            "forward_samples_ms": [r["forward_ms"] for r in runs],
            "raw_scores": [r["scores"] for r in runs],
            **score_stability([r["scores"] for r in runs], ids),
            "logits_shapes": runs[0]["logits_shapes"], "token_stats": runs[0]["token_stats"],
            "gpu_before": before, "gpu_after": gpu_snapshot(torch),
            "device_snapshot_peak_bytes": max(s["device_used_bytes"] for s in all_snapshots),
            "device_snapshot_peak_scope": "before_and_after_microbatch_boundary_samples"}


def _embedding_experiment(torch, settings, samples):
    from app.retrieval.embeddings import get_embedding_provider
    torch.cuda.reset_peak_memory_stats()
    before = gpu_snapshot(torch)
    provider = get_embedding_provider(settings)
    other = get_embedding_provider(settings)
    factory_creates_new = provider is not other
    del other  # This second factory instance has never loaded a model.
    query = load_cases()[0][0].query
    started = time.perf_counter()
    result = provider.encode_query(query)
    torch.cuda.synchronize()
    cold_ms = (time.perf_counter() - started) * 1000
    if not result.device.startswith("cuda") or result.embedding_dim != 1024:
        raise ProbeContractError("embedding_not_resident_cuda_1024")
    times = []
    for _ in range(samples):
        torch.cuda.synchronize()
        start = time.perf_counter()
        provider.encode_query(query)
        torch.cuda.synchronize()
        times.append((time.perf_counter() - start) * 1000)
    report = {"model_id": settings.embedding_model, "embedding_dim": result.embedding_dim,
              "device": result.device, "factory_creates_new_instance": factory_creates_new,
              "lifecycle": "one_explicitly_held_instance_not_production_RAG_lifecycle",
              "cold_load_and_first_query_ms": cold_ms, "warm_query": summarize_latencies(times),
              "warm_query_samples_ms": times, "gpu_before": before, "gpu_after": gpu_snapshot(torch)}
    return provider, report


def run_real_probe(args):
    def run():
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        settings = _settings()
        if not torch.cuda.is_available():
            raise ProbeContractError("cuda_unavailable")
        if args.samples < MIN_SAMPLES or args.warmups < 1:
            raise ProbeContractError("insufficient_benchmark_samples")
        if args.dtype == "bfloat16" and not torch.cuda.is_bf16_supported():
            raise ProbeContractError("bf16_unsupported")
        identity = json.loads(Path(args.identity).read_text(encoding="utf-8"))
        if identity["model_id"] != MODEL_ID:
            raise ProbeContractError("model_identity_mismatch")
        local = Path(settings.reranker_model_path)
        for entry in identity["files"]:
            target = local / entry["name"]
            if target.stat().st_size != entry["size_bytes"] or _sha(target) != entry["sha256"]:
                raise ProbeContractError("model_file_identity_mismatch")
        torch.manual_seed(0)
        torch.cuda.synchronize()
        report = {"status": "running", "model_id": MODEL_ID, "revision": identity["revision"],
                  "mode": args.mode, "dtype": args.dtype, "samples_per_cell": args.samples,
                  "warmups_per_cell": args.warmups, "runtime": _runtime_evidence(torch),
                  "started_at_utc": datetime.now(timezone.utc).isoformat(), "gpu_initial": gpu_snapshot(torch),
                  "retrieval_integration": "not_measured_M0_candidate_simulation_only",
                  "busy_timeout": "not_implemented_M1_scope", "cells": []}
        short, pool = load_cases()
        sensitive = [c.query for c in short] + [c.passage for c in short + pool] + [str(local)]
        embedding = None
        if args.mode in ("embedding", "coexist"):
            embedding, report["embedding"] = _embedding_experiment(torch, settings, args.samples)
            held_model_id = id(embedding._model)
            if args.mode == "embedding":
                report.update(status="ok", gpu_final=gpu_snapshot(torch))
                _write(args.output, report, sensitive=sensitive)
                print(json.dumps({"status": "ok", "mode": args.mode}), flush=True)
                return

        model_config = json.loads((local / "config.json").read_text(encoding="utf-8"))
        if model_config.get("auto_map") or model_config.get("architectures") != ["XLMRobertaForSequenceClassification"]:
            raise ProbeContractError("model_architecture_requires_review")
        start = time.perf_counter()
        tokenizer = AutoTokenizer.from_pretrained(local, local_files_only=True, trust_remote_code=False, use_fast=True)
        tokenizer_load_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        model = AutoModelForSequenceClassification.from_pretrained(local, local_files_only=True,
                    trust_remote_code=False, use_safetensors=True, dtype=getattr(torch, args.dtype))
        from_pretrained_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        model.to("cuda:0")
        model.eval()
        torch.cuda.synchronize()
        to_device_ms = (time.perf_counter() - start) * 1000
        report["load"] = {"success": True, "tokenizer_load_ms": tokenizer_load_ms,
            "from_pretrained_ms": from_pretrained_ms, "to_device_ms": to_device_ms,
            "parameter_dtype": str(next(model.parameters()).dtype),
            "attention_implementation": model.config._attn_implementation,
            "tokenizer_class": type(tokenizer).__name__, "is_fast": tokenizer.is_fast,
            "model_max_length": tokenizer.model_max_length,
            "pair_special_tokens": tokenizer.num_special_tokens_to_add(pair=True),
            "gpu_after_load": gpu_snapshot(torch)}
        counts = token_counts(tokenizer, short + pool)
        first = _score_once(torch, tokenizer, model, short, 512, len(short), counts)
        report["cold_first_forward"] = {k: first[k] for k in ("wall_ms", "forward_ms", "logits_shapes")}
        sanity_runs = [first["scores"]]
        for _ in range(4):
            sanity_runs.append(_score_once(torch, tokenizer, model, short, 512, len(short), counts)["scores"])
        report["sanity"] = {"raw_scores": sanity_runs, "candidate_ids": [c.candidate_id for c in short],
            "relevant_beats_irrelevant": [sanity_runs[0][i] > sanity_runs[0][i + 1] for i in range(0, len(short), 2)],
            **score_stability(sanity_runs, [c.candidate_id for c in short])}
        overlong = Candidate("overlong-query", short[0].query * 200, short[0].passage, "query_over_budget")
        try:
            prepare_pairs(tokenizer, [overlong], max_length=512)
        except ProbeContractError as exc:
            if str(exc) != "query_exceeds_budget":
                raise
            report["overlong_query_rejected_before_forward"] = True
        else:
            raise ProbeContractError("overlong_query_was_not_rejected")
        _write(args.output, report, sensitive=sensitive)
        for length in args.max_lengths:
            for count in args.counts:
                batches = batch_sizes_for(count, args.batch_sizes)
                for batch in batches:
                    cell = _benchmark_cell(torch, tokenizer, model, pool[:count], length, batch,
                                           counts, args.samples, args.warmups)
                    report["cells"].append(cell)
                    if embedding is not None:
                        if id(embedding._model) != held_model_id:
                            raise ProbeContractError("embedding_residency_changed")
                        embedding.encode_query(short[0].query)
                        torch.cuda.synchronize()
                    _write(args.output, report, sensitive=sensitive)
                    print(json.dumps({"mode": args.mode, "dtype": args.dtype, "length": length,
                        "C": count, "batch": batch, "status": cell["status"],
                        "p95_ms": cell.get("warm_wall", {}).get("p95_ms")}), flush=True)
        report.update(status="ok", gpu_final=gpu_snapshot(torch),
                      embedding_kept_loaded=embedding is not None,
                      finished_at_utc=datetime.now(timezone.utc).isoformat())
        _write(args.output, report, sensitive=sensitive)
    return run_gated(run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("download", "reranker", "embedding", "coexist"), default="reranker")
    parser.add_argument("--dtype", choices=("float32", "float16", "bfloat16"), default="float32")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--counts", nargs="+", type=int, choices=(8, 16, 32), default=[8, 16, 32])
    parser.add_argument("--max-lengths", nargs="+", type=int, choices=(512, 1024), default=[512, 1024])
    parser.add_argument("--batch-sizes", nargs="+", type=int, choices=(8, 16, 32), default=None)
    parser.add_argument("--identity", default=str(BACKEND.parent / "docs/phase-12-m0-results/model-identity.json"))
    parser.add_argument("--output", default=str(BACKEND.parent / "docs/phase-12-m0-results/probe.json"))
    args = parser.parse_args(argv)
    try:
        if args.mode == "download":
            download_model(args.output)
        else:
            run_real_probe(args)
    except ProbeDisabled:
        print(json.dumps({"status": "disabled", "real_load_count": 0}), flush=True)
        return 0
    except Exception as exc:
        # Do not expose library exception text: it can contain local paths or inputs.
        result = {"status": "PHASE12_M0_SCOPE_CONFLICT", "error_type": type(exc).__name__}
        if isinstance(exc, ProbeContractError):
            result["reason"] = str(exc)
        print(json.dumps(result), flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
