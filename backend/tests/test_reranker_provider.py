from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from importlib import import_module
import logging
import subprocess
import sys
from threading import Event, Thread
from time import monotonic

import pytest

from tests.test_reranker_config import settings
from tests.test_reranker_runtime import domain, request, runtime


class FakeProvider:
    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.calls = []
        self.closed = 0
        self.block = False
        self.transform = lambda result: result

    def rerank(self, req, *, budget):
        self.calls.append((req, budget))
        self.entered.set()
        if self.block:
            assert self.release.wait(5)
        d = domain()
        result = d.RerankResult(req.request_id, tuple(
            d.RerankScore(c.chunk_id, c.original_rank, 9.0 if c.original_rank else -1.0)
            for c in req.candidates
        ))
        return self.transform(result)

    def close(self):
        self.closed += 1


def service(provider, **overrides):
    return import_module("app.services.reranking").RerankingService(
        settings(**overrides), provider_factory=lambda cfg: provider,
    )


def test_disabled_and_incomplete_profile_do_not_construct_provider():
    module = import_module("app.services.reranking")
    calls = []
    for cfg, expected in [(settings(reranker_enabled=False), "disabled"),
                          (settings(reranker_batch_size=None), "configuration_invalid"),
                          (settings(reranker_provider="local_qwen3"), "configuration_invalid")]:
        svc = module.RerankingService(cfg, provider_factory=lambda cfg: calls.append(cfg))
        assert svc.rerank(request()).failure_reason == expected
        svc.close()
    assert not calls


def test_no_candidates_does_not_create_worker_or_provider():
    module = import_module("app.services.reranking")
    svc = module.RerankingService(settings(), provider_factory=lambda cfg: pytest.fail("load"))
    assert svc.rerank(request(0)).scores == ()
    svc.close()


def test_scores_sorted_by_raw_then_original_rank_then_chunk_id():
    fake = FakeProvider()
    svc = service(fake)
    d = domain()
    req = d.RerankRequest("req", "query", (d.RerankCandidate("z", 2, "c"),
        d.RerankCandidate("a", 2, "c"), d.RerankCandidate("b", 0, "c")))
    result = svc.rerank(req)
    assert result.failure_reason is None
    assert [score.chunk_id for score in result.scores] == ["a", "z", "b"]
    svc.close()


@pytest.mark.parametrize("kind", ["count", "identity", "rank", "duplicate", "request", "nan", "inf", "partial_failure"])
def test_whole_result_validated_before_applying(kind):
    fake = FakeProvider()
    d = domain()

    def corrupt(result):
        scores = list(result.scores)
        if kind == "count":
            scores.pop()
        elif kind == "identity":
            scores[0] = replace(scores[0], chunk_id="foreign")
        elif kind == "rank":
            scores[0] = replace(scores[0], original_rank=999)
        elif kind == "duplicate":
            scores[1] = scores[0]
        elif kind == "request":
            return replace(result, request_id="other")
        elif kind in ("nan", "inf"):
            scores[0] = replace(scores[0], raw_score=float(kind))
        elif kind == "partial_failure":
            return replace(result, failure_reason="oom")
        return d.RerankResult(result.request_id, tuple(scores))

    fake.transform = corrupt
    svc = service(fake)
    result = svc.rerank(request())
    assert result.failure_reason is not None and result.scores == ()
    svc.close()


def test_duplicate_candidate_identity_rejected_without_model_call():
    fake = FakeProvider()
    svc = service(fake)
    req = request()
    result = svc.rerank(replace(req, candidates=(req.candidates[0], req.candidates[0])))
    assert result.failure_reason == "invalid_request" and not fake.calls
    svc.close()


def test_one_worker_busy_timeout_late_discard_then_reuse():
    fake = FakeProvider()
    fake.block = True
    svc = service(fake, reranker_timeout_seconds=0.04)
    try:
        start = monotonic()
        first = svc.rerank(request(request_id="same-id"))
        assert monotonic() - start < 0.5
        assert fake.entered.is_set()
        assert first.failure_reason == "timeout" and first.scores == ()
        first_budget = fake.calls[0][1]
        assert first_budget.cancelled.is_set()
        with ThreadPoolExecutor(max_workers=12) as pool:
            busy = list(pool.map(lambda _: svc.rerank(request()), range(30)))
        assert all(result.failure_reason == "busy" for result in busy)
        assert len(fake.calls) == 1  # no queued requests after timeout
        fake.block = False
        fake.release.set()
        assert svc.wait_idle(2)
        second = svc.rerank(request(request_id="same-id"))
        assert second.failure_reason is None
        assert fake.calls[1][1].token != first_budget.token
        assert first.scores == ()
    finally:
        fake.release.set()
        svc.close()
    assert fake.closed == 1


def test_busy_while_first_load_is_running_and_model_loaded_once(tmp_path):
    module = import_module("app.retrieval.local_cross_encoder")
    svc_module = import_module("app.services.reranking")
    real, tokenizer, model, torch, _ = runtime(tmp_path)
    real.close()
    entered, release = Event(), Event()
    loads = []

    def load(cfg):
        loads.append(cfg)
        entered.set()
        assert release.wait(5)
        return module.LoadedCrossEncoder(tokenizer, model, torch)

    svc = svc_module.RerankingService(settings(reranker_model_path=str(tmp_path)),
        provider_factory=lambda cfg: module.LocalCrossEncoderProvider(cfg, model_loader=load))
    results = []
    first = Thread(target=lambda: results.append(svc.rerank(request())))
    first.start()
    try:
        assert entered.wait(2)
        start = monotonic()
        assert svc.rerank(request()).failure_reason == "busy"
        assert monotonic() - start < 0.1
        assert len(loads) == 1
    finally:
        release.set()
        first.join(2)
    assert results[0].failure_reason is None
    assert svc.rerank(request()).failure_reason is None and len(loads) == 1
    svc.close()


def test_close_during_task_stops_admission_waits_and_is_idempotent():
    fake = FakeProvider()
    fake.block = True
    svc = service(fake, reranker_timeout_seconds=0.03)
    assert svc.rerank(request()).failure_reason == "timeout"
    closer = Thread(target=svc.close)
    closer.start()
    try:
        assert svc.wait_closed(0.1) is False
        assert svc.rerank(request()).failure_reason == "closed"
        assert fake.closed == 0
    finally:
        fake.release.set()
        closer.join(2)
    assert not closer.is_alive() and fake.closed == 1
    svc.close()
    assert fake.closed == 1 and svc.wait_closed(0)


def test_cached_singleton_is_retained_after_close(monkeypatch):
    module = import_module("app.services.reranking")
    monkeypatch.setattr(module, "_service_cache", None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        providers = list(pool.map(lambda _: module.get_reranking_service(settings()), range(20)))
    assert all(item is providers[0] for item in providers)
    module.close_reranking_service()
    module.close_reranking_service()
    assert module.get_reranking_service(settings()) is providers[0]
    assert providers[0].rerank(request()).failure_reason == "closed"


def test_factory_exception_is_unavailable_and_never_retried():
    module = import_module("app.services.reranking")
    calls = []

    def factory(cfg):
        calls.append(1)
        raise RuntimeError("secret path")

    svc = module.RerankingService(settings(), provider_factory=factory)
    for _ in range(3):
        assert svc.rerank(request()).failure_reason == "unavailable"
    assert calls == [1]
    svc.close()


def test_safe_diagnostics_and_repr_exclude_query_content_path(caplog):
    fake = FakeProvider()
    svc = service(fake, reranker_model_path="D:/SECRET-WEIGHTS")
    req = request(query="SECRET-QUERY", content="SECRET-PASSAGE")
    with caplog.at_level(logging.INFO, logger="app.services.reranking"):
        result = svc.rerank(req)
    diagnostic = caplog.text + repr(req) + repr(result)
    assert "SECRET" not in diagnostic
    assert "candidate_count" in caplog.text
    svc.close()


def test_import_and_construction_never_import_ml_runtime():
    code = '''
import importlib.abc
import sys
class RejectML(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in ('torch', 'transformers', 'sentence_transformers'):
            raise AssertionError('ML import attempted')
sys.meta_path.insert(0, RejectML())
from app.core.config import Settings
from app.services.reranking import RerankingService
for enabled in (False, True):
    service = RerankingService(Settings(_env_file=None, reranker_enabled=enabled))
    service.close()
assert not any(name in sys.modules for name in ('torch', 'transformers', 'sentence_transformers'))
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_request_and_result_snapshot_mutable_lists():
    d = domain()
    candidates = [d.RerankCandidate("a", 0, "body")]
    req = d.RerankRequest("req", "query", candidates)
    candidates.clear()
    assert len(req.candidates) == 1
    scores = [d.RerankScore("a", 0, 1.0)]
    result = d.RerankResult("req", scores)
    scores.clear()
    assert len(result.scores) == 1


def test_provider_exception_and_unknown_reason_never_escape_or_leak(caplog):
    fake = FakeProvider()
    svc = service(fake)
    for hook in (
        lambda result: (_ for _ in ()).throw(RuntimeError("SECRET credentials")),
        lambda result: replace(result, failure_reason="SECRET upstream"),
    ):
        fake.transform = hook
        with caplog.at_level(logging.INFO, logger="app.services.reranking"):
            result = svc.rerank(request())
        assert result.failure_reason in ("inference_exception", "invalid_output")
        assert result.scores == ()
        assert "SECRET" not in caplog.text + repr(result)
    svc.close()
