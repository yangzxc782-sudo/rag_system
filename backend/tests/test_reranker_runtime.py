from contextlib import contextmanager
from importlib import import_module
from time import monotonic
from types import SimpleNamespace
import weakref

import pytest

from tests.test_reranker_config import config


def domain():
    return import_module("app.retrieval.reranker")


def request(count=3, *, request_id="request", query="WCB impact", content="passage"):
    d = domain()
    return d.RerankRequest(request_id, query, tuple(
        d.RerankCandidate(f"chunk-{i}", i, content) for i in range(count)
    ))


def budget(seconds=5, **kwargs):
    return domain().RerankBudget(monotonic() + seconds, **kwargs)


class FakeOOM(RuntimeError):
    pass


class FakeTensor:
    def __init__(self, values, shape=None):
        self.values = values
        self.shape = shape or (len(values), 1)
        self.ndim = len(self.shape)

    def __getitem__(self, key):
        assert key == (slice(None), 0)
        return FakeTensor(self.values, (len(self.values),))

    def detach(self):
        return self

    def float(self):
        return self

    def cpu(self):
        return self

    def tolist(self):
        return self.values


class FakeRows:
    def __init__(self, rows):
        self.rows = rows

    def tolist(self):
        return self.rows


class FakeBatch(dict):
    def __init__(self, queries, passages, corrupt=False):
        self.ids = [[None] + [0] * len(q) + [None, None] + [1] * len(p) + [None]
                    for q, p in zip(queries, passages)]
        rows = [[0] + q + [0, 0] + p + [0] for q, p in zip(queries, passages)]
        if corrupt:
            rows[0][1] = -999
        super().__init__(input_ids=FakeRows(rows))
        self.device = None

    def sequence_ids(self, index):
        return self.ids[index]

    def to(self, device):
        self.device = device
        return self


class FakeTokenizer:
    def __init__(self):
        self.calls = []
        self.corrupt = False

    def encode(self, text, *, add_special_tokens, truncation):
        assert not add_special_tokens and not truncation
        return list(map(ord, text))

    def num_special_tokens_to_add(self, pair):
        assert pair
        return 4

    def __call__(self, queries, passages, **kwargs):
        assert kwargs == dict(padding=True, truncation="only_second",
                              max_length=kwargs["max_length"], return_tensors="pt")
        self.calls.append((list(queries), list(passages), kwargs))
        q = [list(map(ord, item)) for item in queries]
        p = [list(map(ord, item))[:kwargs["max_length"] - len(query) - 4]
             for query, item in zip(q, passages)]
        return FakeBatch(q, p, self.corrupt)


class FakeTorch:
    float16 = "torch.float16"
    bfloat16 = "torch.bfloat16"

    def __init__(self):
        self.inference_active = False
        self.synchronizations = 0
        self.cuda = SimpleNamespace(
            OutOfMemoryError=FakeOOM, is_available=lambda: True,
            is_bf16_supported=lambda: True, synchronize=self.synchronize,
            empty_cache=lambda: None,
        )

    def synchronize(self, device=None):
        self.synchronizations += 1

    @contextmanager
    def inference_mode(self):
        assert not self.inference_active
        self.inference_active = True
        try:
            yield
        finally:
            self.inference_active = False


class FakeModel:
    def __init__(self, torch):
        self.torch = torch
        self.calls = 0
        self.evals = 0
        self.devices = []
        self.hook = None

    def to(self, device):
        self.devices.append(device)
        return self

    def eval(self):
        self.evals += 1
        return self

    def __call__(self, input_ids, **kwargs):
        assert self.torch.inference_active
        assert self.evals == 1
        self.calls += 1
        if self.hook:
            return self.hook(len(input_ids.rows))
        return SimpleNamespace(logits=FakeTensor([float(i) - 4.5 for i in range(len(input_ids.rows))]))


def runtime(tmp_path, **overrides):
    module = import_module("app.retrieval.local_cross_encoder")
    torch = FakeTorch()
    tokenizer = FakeTokenizer()
    model = FakeModel(torch)
    loads = []

    def loader(cfg):
        loads.append(cfg)
        return module.LoadedCrossEncoder(tokenizer, model, torch)

    provider = module.LocalCrossEncoderProvider(
        config(reranker_model_path=str(tmp_path), **overrides), model_loader=loader,
    )
    return provider, tokenizer, model, torch, loads


def test_lazy_load_once_and_close_before_load(tmp_path):
    provider, _, model, _, loads = runtime(tmp_path)
    assert loads == []
    provider.close()
    provider.close()
    assert loads == []
    assert provider.rerank(request(), budget=budget()).failure_reason == "closed"
    provider, _, model, _, loads = runtime(tmp_path)
    for _ in range(2):
        assert provider.rerank(request(), budget=budget()).failure_reason is None
    assert len(loads) == 1
    assert model.evals == 1 and model.devices == ["cuda"]
    provider.close()


@pytest.mark.parametrize("count", [0, 1, 9])
def test_exactly_n_raw_scores_with_original_mapping(tmp_path, count):
    provider, _, _, _, loads = runtime(tmp_path)
    req = request(count)
    result = provider.rerank(req, budget=budget())
    assert result.request_id == req.request_id
    assert result.failure_reason is None
    assert len(result.scores) == count
    assert [(s.chunk_id, s.original_rank) for s in result.scores] == [
        (c.chunk_id, c.original_rank) for c in req.candidates
    ]
    if count:
        assert result.scores[0].raw_score == -4.5  # raw logit, no sigmoid
    else:
        assert loads == []
    provider.close()


@pytest.mark.parametrize("shape,values", [
    ((3,), [1, 2, 3]), ((1, 3), [1, 2, 3]), ((3, 2), [1, 2, 3]),
    ((3, 1, 1), [1, 2, 3]), ((2, 1), [1, 2]),
    ((3, 1), [1, float("nan"), 2]), ((3, 1), [1, float("inf"), 2]),
    ((3, 1), [1, float("-inf"), 2]), ((3, 1), [1, 2]),
])
def test_invalid_logits_discard_every_score(tmp_path, shape, values):
    provider, _, model, _, _ = runtime(tmp_path)
    model.hook = lambda _: SimpleNamespace(logits=FakeTensor(values, shape))
    result = provider.rerank(request(), budget=budget())
    assert result.failure_reason == "invalid_output"
    assert result.scores == ()
    provider.close()


@pytest.mark.parametrize("length", [512, 1024])
def test_only_passage_truncates_and_query_is_verified(tmp_path, length):
    provider, tokenizer, _, _, _ = runtime(tmp_path, reranker_max_length=length)
    req = request(1, content="long table row | value |\n" * 200)
    original = req.candidates[0].content
    assert provider.rerank(req, budget=budget()).failure_reason is None
    assert tokenizer.calls[0][0] == [req.query]
    assert tokenizer.calls[0][1] == [original]
    assert req.candidates[0].content == original
    tokenizer.corrupt = True
    assert provider.rerank(req, budget=budget()).failure_reason == "query_truncated"
    provider.close()


def test_query_too_long_never_forwards(tmp_path):
    provider, _, model, _, _ = runtime(tmp_path)
    result = provider.rerank(request(query="Q" * 509), budget=budget())
    assert result.failure_reason == "query_too_long"
    assert model.calls == 0
    provider.close()


@pytest.mark.parametrize("failure", [FakeOOM("private weights path"), RuntimeError("private query")])
def test_later_microbatch_failure_is_all_or_nothing_without_auto_fallback(tmp_path, failure):
    provider, tokenizer, model, torch, loads = runtime(tmp_path, reranker_batch_size=2)

    def forward(count):
        if model.calls == 2:
            raise failure
        return SimpleNamespace(logits=FakeTensor([9.0] * count))

    model.hook = forward
    result = provider.rerank(request(6), budget=budget())
    assert result.scores == ()
    assert result.failure_reason == ("oom" if isinstance(failure, FakeOOM) else "inference_exception")
    assert model.calls == 2 and len(tokenizer.calls) == 2 and len(loads) == 1
    assert model.devices == ["cuda"] and loads[0].dtype == "fp16"
    assert loads[0].batch_size == 2 and loads[0].max_length == 512
    assert torch.synchronizations > 0
    provider.close()


def test_expiry_between_microbatches_stops_next_batch(tmp_path):
    provider, _, model, _, _ = runtime(tmp_path, reranker_batch_size=1)
    task_budget = budget()

    def forward(count):
        task_budget.cancelled.set()
        return SimpleNamespace(logits=FakeTensor([1.0] * count))

    model.hook = forward
    result = provider.rerank(request(), budget=task_budget)
    assert result.failure_reason == "timeout" and result.scores == ()
    assert model.calls == 1
    provider.close()


def test_unhealthy_cuda_latches_unavailable(tmp_path):
    provider, _, model, torch, loads = runtime(tmp_path)

    def forward(_):
        torch.cuda.synchronize = lambda *a: (_ for _ in ()).throw(RuntimeError("device lost"))
        raise FakeOOM()

    model.hook = forward
    assert provider.rerank(request(), budget=budget()).scores == ()
    result = provider.rerank(request(), budget=budget())
    assert result.failure_reason == "unavailable" and model.calls == 1 and len(loads) == 1
    provider.close()


def test_missing_directory_never_calls_loader(tmp_path):
    module = import_module("app.retrieval.local_cross_encoder")
    calls = []
    provider = module.LocalCrossEncoderProvider(
        config(reranker_model_path=str(tmp_path / "missing")), model_loader=lambda cfg: calls.append(cfg),
    )
    assert provider.rerank(request(), budget=budget()).failure_reason == "unavailable"
    assert not calls
    provider.close()


def test_load_failure_has_bounded_retry_and_safe_result(tmp_path):
    module = import_module("app.retrieval.local_cross_encoder")
    calls = []

    def loader(cfg):
        calls.append(1)
        raise RuntimeError("SECRET query and D:/weights")

    provider = module.LocalCrossEncoderProvider(config(reranker_model_path=str(tmp_path)), model_loader=loader)
    for _ in range(3):
        result = provider.rerank(request(), budget=budget())
        assert result.failure_reason == "unavailable" and "SECRET" not in repr(result)
    assert calls == [1]  # explicit restart-only retry policy
    provider.close()


@pytest.mark.parametrize("dtype", ["fp16", "bf16"])
def test_default_loader_local_only_safe_contract_without_real_ml(tmp_path, monkeypatch, dtype):
    module = import_module("app.retrieval.local_cross_encoder")
    torch = FakeTorch()
    tokenizer, model = FakeTokenizer(), FakeModel(torch)
    calls = []

    def factory(kind, result):
        def load(path, **kwargs):
            calls.append((kind, path, kwargs))
            return result
        return SimpleNamespace(from_pretrained=load)

    transformers = SimpleNamespace(
        AutoTokenizer=factory("tokenizer", tokenizer),
        AutoModelForSequenceClassification=factory("model", model),
    )
    monkeypatch.setattr(module, "import_module", lambda name: {"torch": torch, "transformers": transformers}[name])
    provider = module.LocalCrossEncoderProvider(config(reranker_model_path=str(tmp_path), reranker_dtype=dtype))
    assert not calls
    assert provider.rerank(request(), budget=budget()).failure_reason is None
    assert len(calls) == 2
    for _, path, kwargs in calls:
        assert path == str(tmp_path.resolve())
        assert kwargs["local_files_only"] is True
        assert kwargs["trust_remote_code"] is False
    assert calls[1][2]["dtype"] == (torch.float16 if dtype == "fp16" else torch.bfloat16)
    assert calls[1][2]["use_safetensors"] is True
    provider.close()


def test_oom_releases_forward_frame_tensors_before_cache_cleanup(tmp_path):
    provider, _, model, torch, _ = runtime(tmp_path)
    refs, cleanup_saw_released = [], []

    class Activation:
        pass

    def forward(_):
        activation = Activation()
        refs.append(weakref.ref(activation))
        raise FakeOOM()

    model.hook = forward
    torch.cuda.empty_cache = lambda: cleanup_saw_released.append(refs[0]() is None)
    assert provider.rerank(request(), budget=budget()).failure_reason == "oom"
    assert cleanup_saw_released == [True]
    provider.close()


def test_deadline_expiry_without_explicit_cancellation_stops_microbatches(tmp_path):
    provider, _, model, _, _ = runtime(tmp_path, reranker_batch_size=1)
    clock_value = [0.0]
    task_budget = domain().RerankBudget(1.0, clock=lambda: clock_value[0])

    def forward(count):
        clock_value[0] = 2.0
        return SimpleNamespace(logits=FakeTensor([1.0] * count))

    model.hook = forward
    result = provider.rerank(request(), budget=task_budget)
    assert result.failure_reason == "timeout" and result.scores == ()
    assert not task_budget.cancelled.is_set() and model.calls == 1
    provider.close()


def test_expired_budget_before_load_performs_zero_loads(tmp_path):
    provider, _, _, _, loads = runtime(tmp_path)
    assert provider.rerank(request(), budget=budget(-1)).failure_reason == "timeout"
    assert not loads
    provider.close()


def test_cancellation_during_load_skips_tokenizer_and_forward(tmp_path):
    module = import_module("app.retrieval.local_cross_encoder")
    torch, tokenizer = FakeTorch(), FakeTokenizer()
    model = FakeModel(torch)
    task_budget = budget()

    def loader(cfg):
        task_budget.cancelled.set()
        return module.LoadedCrossEncoder(tokenizer, model, torch)

    provider = module.LocalCrossEncoderProvider(config(reranker_model_path=str(tmp_path)), model_loader=loader)
    assert provider.rerank(request(), budget=task_budget).failure_reason == "timeout"
    assert not tokenizer.calls and model.calls == 0
    assert provider.rerank(request(), budget=budget()).failure_reason is None
    provider.close()


@pytest.mark.parametrize("unavailable", ["cuda", "bf16"])
def test_unavailable_device_or_dtype_never_loads_or_switches(tmp_path, monkeypatch, unavailable):
    module = import_module("app.retrieval.local_cross_encoder")
    torch = FakeTorch()
    if unavailable == "cuda":
        torch.cuda.is_available = lambda: False
    else:
        torch.cuda.is_bf16_supported = lambda: False
    imports = []

    def fake_import(name):
        imports.append(name)
        assert name == "torch"
        return torch

    monkeypatch.setattr(module, "import_module", fake_import)
    provider = module.LocalCrossEncoderProvider(config(reranker_model_path=str(tmp_path), reranker_dtype="bf16"))
    for _ in range(2):
        assert provider.rerank(request(), budget=budget()).failure_reason == "unavailable"
    assert imports == ["torch"]
    provider.close()


def test_healthy_oom_recovers_only_on_next_request_with_same_runtime(tmp_path):
    provider, _, model, _, loads = runtime(tmp_path)
    model.hook = lambda _: (_ for _ in ()).throw(FakeOOM())
    assert provider.rerank(request(), budget=budget()).failure_reason == "oom"
    model.hook = None
    assert provider.rerank(request(), budget=budget()).failure_reason is None
    assert len(loads) == 1 and model.calls == 2 and model.devices == ["cuda"]
    provider.close()
