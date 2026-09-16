"""M3 production-provider smoke, opt-in through the existing M0 real-model gate.

No downloads, real storage, LLM or Graph queries. Imports are ML-free until gated.
The fixed FP16/C8/batch8/512 profile comes from M0 coexist-float16 evidence.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import gc
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import platform
import sys
from threading import Event, Thread
from time import perf_counter
from unittest.mock import patch
import weakref

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from tests.phase12_local.bge_probe import (
    ProbeDisabled, gpu_snapshot, load_cases, run_gated, sanitize_report,
    score_stability, summarize_latencies, validate_gpu_metrics,
)

MIB = 1024 * 1024
SAMPLES = 20
NORMAL_DEADLINE = 5.0  # M0's explicit proposed test budget, not a production default.
SHORT_DEADLINE = 0.050  # Failure injection only; normal, bounded real forward input.


class SmokeFailure(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise SmokeFailure(reason)


class MetricRecorder:
    def __init__(self):
        self.samples = []
        self.gpu = []

    def add(self, wall_ms, metrics):
        self.gpu.append(validate_gpu_metrics(metrics))
        self.samples.append(wall_ms)

    def summary(self):
        summary = summarize_latencies(self.samples)
        return {**summary, "samples_ms": self.samples,
                "allocated_peak_bytes": max(r["allocated_peak_bytes"] for r in self.gpu),
                "reserved_peak_bytes": max(r["reserved_peak_bytes"] for r in self.gpu),
                "device_snapshot_peak_bytes": max(r["device_used_bytes"] for r in self.gpu)}


def score_record(request, result):
    from app.retrieval.reranker import validate_result
    require(result.failure_reason is None, "provider_" + str(result.failure_reason))
    try:
        validate_result(request, result)
    except Exception:
        raise SmokeFailure("score_identity_or_finite") from None
    by_id = {s.chunk_id: s.raw_score for s in result.scores}
    return {"candidate_ids": [c.chunk_id for c in request.candidates],
            "raw_scores": [by_id[c.chunk_id] for c in request.candidates],
            "ranking": [s.chunk_id for s in result.scores]}


def validate_timeline(row):
    require(0 <= row["forward_start_ms"] < row["timeout_return_ms"]
            <= row["busy_return_ms"] < row["forward_end_ms"] <= row["idle_ms"]
            < row["recovery_ms"], "timeout_busy_timeline")
    require(row["deadline_ms"] <= row["timeout_return_ms"] <= row["deadline_ms"] + 100,
            "timeout_slo")
    require(row["busy_reason"] == "busy" and row["recovery_reason"] is None
            and row["max_active"] == 1, "worker_recovery")


def validate_late_result(row):
    require(row["cancelled"] and not row["late_result"] and not row["fallback_changed"]
            and row["old_token"] != row["recovery_token"] and row["recovery_request_matches"],
            "late_result_pollution")


def validate_memory_trend(rows):
    require(len(rows) >= SAMPLES, "insufficient_memory_samples")
    allocated = [validate_gpu_metrics(r)["allocated_bytes"] for r in rows]
    # Compare settled post-request allocations, allowing 1 MiB measurement noise.
    growth = max(allocated[-5:]) - min(allocated[:5])
    require(growth <= MIB, "allocated_memory_growth")
    return {"allocated_growth_bytes": growth, "allocated_min_bytes": min(allocated),
            "allocated_max_bytes": max(allocated), "post_request_samples": rows}


def safe_report(data, *, sensitive=()):
    result = sanitize_report(data, sensitive=sensitive)
    def inspect(value):
        if isinstance(value, dict):
            for item in value.values():
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, str):
            require(not value.startswith(("/", "\\")), "private_report_value")
    inspect(result)
    return result


def run_smoke(output, *, environ=None, operation=None):
    try:
        return run_gated(operation or (lambda: _real_smoke(Path(output))), environ=environ)
    except ProbeDisabled:
        return {"status": "disabled", "real_load_count": 0}


class SmokeLLM:
    def generate(self, request):
        from app.llm.messages import LLMMessage, LLMTextContentPart
        from app.llm.provider import LLMGenerateResult
        return LLMGenerateResult(message=LLMMessage(role="assistant", content=(
            LLMTextContentPart(text="Smoke fixture [1]"),)), provider="local", model="m3-fake")


def smoke_hybrid_sources(candidates):
    from uuid import UUID
    from app.services.hybrid_search import HybridSearchItem
    # Valid public identities; text/profile remain identical to the M0 fixture.
    ordered = [candidates[1], candidates[0], *candidates[2:]]
    return [HybridSearchItem(str(UUID(int=i)), str(UUID(int=100)), "m3-fixture.md",
        i, c.passage, None, "hybrid", 1.0, 1.0, i, i, 1 / (60 + i), [], "Qwen3-Embedding-0.6B", 1024)
        for i, c in enumerate(ordered, 1)]


class DeviceSampler:
    """10 ms device-wide sampling; PyTorch peaks cover allocator transients."""
    def __init__(self, torch):
        self.torch = torch
        self.stop = Event()
        self.samples = []
        self.failed = False
        self.thread = Thread(target=self._run, name="m3-device-sampler", daemon=True)

    def _run(self):
        try:
            while not self.stop.is_set():
                self.samples.append((perf_counter(), gpu_snapshot(self.torch)))
                if self.samples[-1][1]["device_used_bytes"] > 6500 * MIB:
                    self.stop.set()
                self.stop.wait(0.01)
        except Exception:
            self.failed = True
            self.stop.set()

    def check(self):
        require(not self.failed, "PHASE12_M3_CUDA_UNHEALTHY")
        require(not self.samples or max(r[1]["device_used_bytes"] for r in self.samples) <= 6500 * MIB,
                "PHASE12_M3_GPU_SLO_FAILURE")

    def close(self):
        self.stop.set()
        self.thread.join(5)


class Observation:
    """Read-only hooks on the actual production-loaded model, never fake logits.

    The first encoder-layer hook runs after real CUDA embedding operations.
    No artificial sleep or barrier prolongs the real forward.
    """
    def __init__(self, torch, loader):
        self.torch, self.loader = torch, loader
        self.loads = []
        self.models = []  # Weak references do not prolong runtime residency.
        self.forwards = []
        self.entered = Event()
        self.finished = Event()
        self.active = 0
        self.max_active = 0
        self.current = None

    def load(self, config):
        start = perf_counter()
        loaded = self.loader(config)
        self.loads.append({"tokenizer_and_model_load_ms": (perf_counter() - start) * 1000,
                           "parameter_dtype": str(next(loaded.model.parameters()).dtype),
                           "tokenizer_class": type(loaded.tokenizer).__name__,
                           "model_max_length": loaded.tokenizer.model_max_length})
        self.models.append(weakref.ref(loaded.model))
        loaded.model.register_forward_pre_hook(self._start)
        loaded.model.roberta.encoder.layer[0].register_forward_pre_hook(self._entered)
        loaded.model.register_forward_hook(self._finish)
        return loaded

    def reset(self):
        self.entered.clear()
        self.finished.clear()

    def _start(self, model, args):
        require(not model.training and self.torch.is_inference_mode_enabled(), "model_state")
        self.active += 1
        self.max_active = max(self.active, self.max_active)
        require(self.active == 1, "second_cuda_forward")
        event = self.torch.cuda.Event(enable_timing=True)
        event.record()
        self.current = {"start": perf_counter(), "event": event}

    def _entered(self, model, args):
        self.entered.set()

    def _finish(self, model, args, output):
        end = self.torch.cuda.Event(enable_timing=True)
        end.record()
        self.torch.cuda.synchronize()
        row = self.current
        self.forwards.append({"start": row["start"], "end": perf_counter(),
                              "gpu_ms": row["event"].elapsed_time(end),
                              "shape": list(output.logits.shape)})
        self.current = None
        self.active -= 1
        self.finished.set()


def _sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _real_smoke(output):
    # This function is reachable only behind M0's exact gate. No download path exists.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
    import torch
    from fastapi.testclient import TestClient
    from app.core.config import Settings
    from app.main import create_app
    from app.retrieval.embeddings import get_embedding_provider
    from app.retrieval import local_cross_encoder as runtime
    from app.retrieval.reranker import RerankCandidate, RerankRequest
    from app.services import reranking

    actual = Settings()
    cfg = Settings(_env_file=None, reranker_enabled=True, reranker_provider="local_transformers",
        reranker_model="BAAI/bge-reranker-v2-m3", reranker_model_path=actual.reranker_model_path,
        reranker_device="cuda", reranker_dtype="fp16", reranker_candidate_limit=8,
        reranker_batch_size=8, reranker_max_length=512, reranker_timeout_seconds=NORMAL_DEADLINE,
        graph_retrieval_enabled=False, document_deletion_executor_enabled=False,
        llm_provider="local", llm_model="m3-fake", llm_base_url="http://localhost:11434/v1")
    report = {"status": "running", "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_id": cfg.reranker_model,
        "profile": {"dtype": "fp16", "C": 8, "batch_size": 8, "max_length": 512,
                    "normal_deadline_seconds": NORMAL_DEADLINE, "timeout_test_seconds": SHORT_DEADLINE,
                    "production_defaults": False},
        "runtime": {"python": platform.python_version(), "cuda_build": torch.version.cuda,
                    "versions": {p: importlib.metadata.version(p) for p in
                                 ("torch", "transformers", "sentence-transformers")}},
        "scope": "production_BGE_with_deterministic_Hybrid_no_real_LLM_Graph_storage"}
    short, pool = load_cases()
    candidates = pool[:8]
    query = candidates[0].query
    sensitive = [c.query for c in short] + [c.passage for c in short + pool] + [actual.reranker_model_path]
    sampler = None
    embedding = None
    observer = Observation(torch, runtime._load_local_model)
    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(safe_report(report, sensitive=sensitive), ensure_ascii=False,
                                     indent=2, allow_nan=False) + "\n", encoding="utf-8")
    def checkpoint(stage):
        if sampler:
            sampler.check()
        report["last_completed_stage"] = stage
        save()
        print(json.dumps({"stage": stage, "status": "ok", "loads": len(observer.loads)}), flush=True)
    def req(identity="warm"):
        return RerankRequest(identity, query, tuple(RerankCandidate(c.candidate_id, i, c.passage)
                                                   for i, c in enumerate(candidates, 1)))
    def timed(svc, request):
        torch.cuda.synchronize()
        start = perf_counter()
        result = svc.rerank(request)
        elapsed = (perf_counter() - start) * 1000  # Do not wait on GPU before measuring timeout return.
        if result.failure_reason is None:
            torch.cuda.synchronize()
        return result, elapsed
    try:
        require(torch.cuda.is_available(), "cuda_unavailable")
        report["runtime"].update(gpu=torch.cuda.get_device_name(0), capability=list(torch.cuda.get_device_capability(0)))
        identity = json.loads((BACKEND.parent / "docs/phase-12-m0-results/model-identity.json").read_text())
        require(identity["model_id"] == cfg.reranker_model, "model_identity")
        local = Path(cfg.reranker_model_path)
        for entry in identity["files"]:
            target = local / entry["name"]
            require(target.stat().st_size == entry["size_bytes"] and _sha(target) == entry["sha256"], "model_hash")
        report.update(revision=identity["revision"], model_files_verified=6, harness_sha256=_sha(__file__))
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        sampler = DeviceSampler(torch)
        sampler.thread.start()
        sampler.check()
        embedding = get_embedding_provider(actual)
        start = perf_counter()
        embedded = embedding.encode_query(query)
        torch.cuda.synchronize()
        require(embedded.device.startswith("cuda") and embedded.embedding_dim == 1024, "embedding_contract")
        held_embedding = id(embedding._model)
        report["embedding"] = {"model": embedded.embedding_model, "device": embedded.device,
            "dim": embedded.embedding_dim, "cold_ms": (perf_counter() - start) * 1000,
            "gpu": gpu_snapshot(torch), "lifecycle": "explicit_single_held_instance_factory_is_not_singleton"}
        checkpoint("embedding_loaded")
        reranking.close_reranking_service()
        with patch.object(runtime, "_load_local_model", observer.load):
            with TestClient(create_app(settings=cfg)):
                require(reranking._service_cache is None and not observer.loads, "startup_loaded")
                svc = reranking.get_reranking_service(cfg)
                require(svc._provider is None and svc._thread is None, "not_lazy")
                first, cold_ms = timed(svc, req("cold"))
                report["cold"] = {"caller_ms": cold_ms, "failure_reason": first.failure_reason}
                require(first.failure_reason is None, "cold_" + str(first.failure_reason))
                score_record(req("cold"), first)
                require(len(observer.loads) == 1, "load_count")
                checkpoint("production_cold_load")
                # Fixed repeated input, including M0's long paragraph/table cases.
                for i in range(3):
                    score_record(req(), timed(svc, req())[0])
                recorder = MetricRecorder()
                runs = []
                for i in range(SAMPLES):
                    result, wall_ms = timed(svc, req(f"warm-{i}"))
                    row = score_record(req(f"warm-{i}"), result)
                    runs.append(row["raw_scores"])
                    recorder.add(wall_ms, gpu_snapshot(torch))
                    require(id(embedding._model) == held_embedding, "embedding_changed")
                    sampler.check()
                report["warm"] = recorder.summary()
                report["stability"] = {**score_stability(runs, row["candidate_ids"]), "raw_scores": runs,
                                       "candidate_ids": row["candidate_ids"]}
                report["memory_trend"] = validate_memory_trend(recorder.gpu)
                require(report["stability"]["ranking_stable"], "PHASE12_M3_SCORE_STABILITY_FAILURE")
                require(report["warm"]["p95_ms"] <= 2000, "warm_slo")
                report["load_once"] = len(observer.loads) == 1
                # Actual production scoring of the four M0 sanity pairs.
                sanity = []
                for i in range(0, len(short), 2):
                    pair = short[i:i + 2]
                    request = RerankRequest(f"sanity-{i}", pair[0].query, tuple(
                        RerankCandidate(c.candidate_id, n, c.passage) for n, c in enumerate(pair, 1)))
                    record = score_record(request, timed(svc, request)[0])
                    require(record["raw_scores"][0] > record["raw_scores"][1], "sanity_direction")
                    sanity.append(record)
                report["sanity"] = sanity
                checkpoint("warm_stability_and_sanity")
                _wiring_and_failures(torch, svc, cfg, candidates, req, observer, report, sampler)
                checkpoint("rag_busy_timeout_recovery")
                require(len(observer.loads) == 1, "reloaded_after_timeout")
            gc.collect()
            require(reranking._service_cache is None and svc.wait_closed(0), "lifespan_cache")
            require(observer.models[0]() is None, "model_reference_after_close")
            report["lifecycle_1"] = {"closed": True, "cache_cleared": True, "model_released": True,
                                      "gpu_after": gpu_snapshot(torch)}
            checkpoint("lifecycle_1_closed")
            client = TestClient(create_app(settings=cfg))
            client.__enter__()
            try:
                fresh = reranking.get_reranking_service(cfg)
                require(fresh is not svc and fresh._provider is None and len(observer.loads) == 1, "fresh_lifecycle")
                score_record(req("lifespan-2"), timed(fresh, req("lifespan-2"))[0])
                require(len(observer.loads) == 2, "second_lifecycle_load_count")
                observer.reset()
                results = []
                caller = Thread(target=lambda: results.append(fresh.rerank(req("shutdown"))))
                caller.start()
                require(observer.entered.wait(5), "shutdown_forward_not_started")
                start = perf_counter()
                closer = Thread(target=lambda: client.__exit__(None, None, None))
                closer.start()
                caller.join(5)
                require(results and results[0].failure_reason == "closed", "shutdown_caller")
                require(fresh.rerank(req("closed")).failure_reason == "closed", "shutdown_admission")
                closer.join(10)
                require(not closer.is_alive() and fresh.wait_closed(0), "shutdown_join")
                require(observer.finished.is_set() and reranking._service_cache is None, "shutdown_clear_before_forward")
                report["lifecycle_2"] = {"fresh": True, "load_count_total": 2, "shutdown_inflight": True,
                    "shutdown_wait_ms": (perf_counter() - start) * 1000,
                    "background_forward_ms": (observer.forwards[-1]["end"] - observer.forwards[-1]["start"]) * 1000,
                    "admission_stopped": True, "cache_cleared": True}
            finally:
                client.__exit__(None, None, None) if reranking._service_cache is not None else None
            gc.collect()
            require(all(model() is None for model in observer.models), "model_reference_after_lifecycle2")
        require(id(embedding._model) == held_embedding, "embedding_changed")
        embedding.encode_query(query)
        torch.cuda.synchronize()
        report["embedding"]["same_model_still_loaded_and_encodes"] = True
        report["loads"] = observer.loads
        report["forward"] = {"count": len(observer.forwards), "max_active": observer.max_active,
            "logits_shapes": sorted({tuple(r["shape"]) for r in observer.forwards}),
            "gpu_samples_ms": [r["gpu_ms"] for r in observer.forwards]}
        require(observer.max_active == 1 and all(r["shape"][1:] == [1] for r in observer.forwards), "forward_contract")
        report["oom"] = False
        checkpoint("lifecycle_2_and_shutdown")
        report["status"] = "ok"
    except Exception as exc:
        report["status"] = "PHASE12_M3_SCOPE_CONFLICT"
        report["failure"] = str(exc) if isinstance(exc, SmokeFailure) else type(exc).__name__
        if any(term in str(exc).lower() for term in ("illegal memory", "device-side assert")):
            report["failure"] = "PHASE12_M3_CUDA_UNHEALTHY"
        # Cleanup only. Never continue model experiments after a failed gate.
    finally:
        reranking.close_reranking_service()
        if sampler:
            sampler.close()
            report["device_sampling"] = {"sample_count": len(sampler.samples), "interval_target_ms": 10,
                "max_interval_ms": max(((b[0] - a[0]) * 1000 for a, b in zip(sampler.samples, sampler.samples[1:])), default=0),
                "device_snapshot_peak_bytes": max((r[1]["device_used_bytes"] for r in sampler.samples), default=0),
                "allocated_peak_bytes": max((r[1]["allocated_peak_bytes"] for r in sampler.samples), default=0),
                "reserved_peak_bytes": max((r[1]["reserved_peak_bytes"] for r in sampler.samples), default=0),
                "sampler_failed": sampler.failed}
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save()
    return {"status": report["status"], "failure": report.get("failure")}


def _wiring_and_failures(torch, svc, cfg, candidates, req, observer, report, sampler):
    from app.schemas.rag import RagAskData
    from app.services import hybrid_search, rag, reranking
    from app.services.hybrid_search import HybridSearchResult
    query = candidates[0].query
    # Deterministic fixture RRF order deliberately puts the first negative before its positive.
    sources = smoke_hybrid_sources(candidates)
    searches, outcomes = [], []
    def search(db, *, query, limit, document_id, settings):
        searches.append(limit)
        return HybridSearchResult(query, limit, len(sources[:limit]), sources[:limit])
    original_optional = rag.optional_rerank_chunks
    def optional(*args, **kwargs):
        outcome = original_optional(*args, **kwargs)
        outcomes.append(outcome)
        return outcome
    def ask(settings=cfg, k=2):
        before = len(searches)
        result = rag.answer_question(object(), query, k, settings=settings, llm_provider=SmokeLLM())
        require(len(searches) == before + 1, "second_hybrid")
        return result
    def selection(settings):
        # Incremental SLO ends after M2 selection, before Context/Graph/LLM.
        start = perf_counter()
        plan = rag._plan_rerank(2, settings)
        snapshot = rag.retrieve_chunks(object(), query, plan.hybrid_limit, None, settings)
        outcome = rag.optional_rerank_chunks(query, snapshot, settings, limit=2, plan=plan)
        return outcome, (perf_counter() - start) * 1000
    with patch.object(hybrid_search, "hybrid_search_chunks", search), patch.object(rag, "optional_rerank_chunks", optional):
        answer = ask()
        require(outcomes[-1].applied and answer.retrieval.items != sources[:2], "real_rag_order")
        require([c.chunk_id for c in answer.citations] == [i.chunk_id for i in answer.retrieval.items], "citation_order")
        public = RagAskData.from_service_result(answer).model_dump(mode="json")
        require(set(public) == {"question", "answer", "context_status", "citations", "retrieval", "llm", "graph"}, "public_schema")
        require(all(not any("rerank" in key for key in item) for item in public["retrieval"]["items"]), "public_score")
        report["rag_wiring"] = {"real_rerank_applied": True, "final_order": [i.chunk_id for i in answer.retrieval.items],
            "citations_match": True, "public_schema_unchanged": True, "hybrid_fixture": True, "llm_fake": True}
        forward_count = len(observer.forwards)
        bypass = ask(k=9)
        require(not outcomes[-1].applied and outcomes[-1].fallback_reason == "public_limit_exceeds_reranker_capacity"
                and len(observer.forwards) == forward_count and bypass.retrieval.items == sources, "capacity_bypass")
        report["rag_wiring"]["k_above_c_zero_forward"] = True
        off = cfg.model_copy(update={"reranker_enabled": False})
        baseline, enabled, incremental = [], [], []
        for _ in range(SAMPLES):
            torch.cuda.synchronize()
            off_out, off_ms = selection(off)
            on_out, on_ms = selection(cfg)
            require(not off_out.applied and on_out.applied, "incremental_fixture")
            baseline.append(off_ms)
            enabled.append(on_ms)
            incremental.append(on_ms - off_ms)
            sampler.check()
        report["incremental"] = {"off": summarize_latencies(baseline), "on": summarize_latencies(enabled),
            "paired_delta": summarize_latencies(incremental), "paired_deltas_ms": incremental}
        require(report["incremental"]["paired_delta"]["p95_ms"] <= 2200, "incremental_slo")
        # Real busy samples, only while the first encoder layer has entered and forward has not completed.
        observer.reset()
        results = []
        thread = Thread(target=lambda: results.append(svc.rerank(req("busy-A"))))
        thread.start()
        require(observer.entered.wait(5), "busy_forward_start")
        busy_times = []
        try:
            for _ in range(SAMPLES):
                require(not observer.finished.is_set(), "busy_observation_window_ended")
                start = perf_counter()
                busy = ask()
                busy_times.append((perf_counter() - start) * 1000)
                require(outcomes[-1].fallback_reason == "busy" and busy.retrieval.items == sources[:2], "busy_fallback")
        finally:
            thread.join(10)
        require(not thread.is_alive() and results[0].failure_reason is None, "busy_A_completion")
        report["busy"] = {**summarize_latencies(busy_times), "samples_ms": busy_times, "real_forward_inflight": True}
        require(report["busy"]["p95_ms"] <= 50, "busy_slo")
        timelines, late_rows = [], []
        for i in range(SAMPLES):
            require(svc.wait_idle(5), "not_idle_before_timeout")
            # Test-only deadline injection while idle. The production wait/worker/budget
            # code is unchanged. Never mutate device/dtype/C/batch/length or switch runtime.
            normal_config = svc._config
            svc._config = replace(normal_config, timeout_seconds=SHORT_DEADLINE)
            observer.reset()
            answers = []
            start = perf_counter()
            def timeout_call():
                answers.append(ask())
                answers.append(perf_counter())
            caller = Thread(target=timeout_call)
            caller.start()
            require(observer.entered.wait(5), "timeout_before_forward")
            with svc._condition:
                task = svc._task
            require(task is not None, "timeout_task_missing")
            caller.join(5)
            require(not caller.is_alive() and len(answers) == 2, "timeout_return_missing")
            timeout_reason = outcomes[-1].fallback_reason
            require(timeout_reason == "timeout" and answers[0].retrieval.items == sources[:2], "timeout_not_fallback")
            frozen_order = tuple(answers[0].retrieval.items)
            busy = ask()
            busy_at = perf_counter()
            busy_reason = outcomes[-1].fallback_reason
            require(busy.retrieval.items == sources[:2] and busy_reason == "busy", "B_after_timeout")
            require(svc.wait_idle(5), "timeout_never_idle")
            idle_at = perf_counter()
            forward = observer.forwards[-1]
            svc._config = normal_config
            c_request = req(f"recovery-{i}")
            # Observe the next budget token without replacing scoring or outcomes.
            budget_tokens = []
            provider = svc._provider
            original = provider.rerank
            def tracked(request, *, budget):
                budget_tokens.append(budget.token)
                return original(request, budget=budget)
            with patch.object(provider, "rerank", tracked):
                recovery = svc.rerank(c_request)
            score_record(c_request, recovery)
            t = {"forward_start_ms": (forward["start"] - start) * 1000,
                "timeout_return_ms": (answers[1] - start) * 1000, "busy_return_ms": (busy_at - start) * 1000,
                "forward_end_ms": (forward["end"] - start) * 1000, "idle_ms": (idle_at - start) * 1000,
                "recovery_ms": (perf_counter() - start) * 1000, "deadline_ms": SHORT_DEADLINE * 1000,
                "busy_reason": busy_reason, "recovery_reason": recovery.failure_reason, "max_active": observer.max_active}
            late = {"cancelled": task.budget.cancelled.is_set(), "late_result": task.result is not None,
                "fallback_changed": tuple(answers[0].retrieval.items) != frozen_order,
                "old_token": task.budget.token, "recovery_token": budget_tokens[0],
                "recovery_request_matches": recovery.request_id == c_request.request_id}
            timelines.append(t)
            late_rows.append(late)
            report["timeout"] = {"timelines": timelines, "late_result_checks": late_rows}
            validate_timeline(t)
            validate_late_result(late)
            sampler.check()
        report["timeout"]["return_latency"] = summarize_latencies([t["timeout_return_ms"] for t in timelines])
        report["timeout"]["background_completion"] = summarize_latencies([t["forward_end_ms"] for t in timelines])
        # A separate unloaded service with an impossible path; real weights remain untouched.
        bad = reranking.RerankingService(cfg.model_copy(update={"reranker_model_path": str(BACKEND / "M3-missing-model.tmp")}))
        try:
            with patch.object(reranking, "get_reranking_service", return_value=bad):
                bad_answer = ask()
                require(outcomes[-1].fallback_reason == "unavailable" and bad_answer.retrieval.items == sources[:2], "load_failure")
            report["load_failure"] = {"injected_missing_path": True, "unavailable": True, "rrf_fallback": True}
        finally:
            bad.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(BACKEND.parent / "docs/phase-12-m3-results/local-smoke.json"))
    args = parser.parse_args(argv)
    result = run_smoke(args.output)
    print(json.dumps(result), flush=True)
    return 0 if result["status"] in ("ok", "disabled") else 2


if __name__ == "__main__":
    raise SystemExit(main())
