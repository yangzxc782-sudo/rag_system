from importlib import import_module
from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def settings(**overrides):
    values = dict(
        _env_file=None, reranker_enabled=True,
        reranker_provider="local_transformers",
        reranker_model="BAAI/bge-reranker-v2-m3",
        reranker_candidate_limit=16, reranker_batch_size=8,
        reranker_max_length=512, reranker_timeout_seconds=2.0,
    )
    values.update(overrides)
    return Settings(**values)


def config(**overrides):
    return import_module("app.retrieval.reranker").RerankerConfig.from_settings(settings(**overrides))


def test_disabled_defaults_do_not_select_an_unapproved_profile():
    value = Settings(_env_file=None)
    assert value.reranker_enabled is False
    assert value.reranker_provider == "local_transformers"
    assert value.reranker_model == "BAAI/bge-reranker-v2-m3"
    assert value.reranker_dtype == "fp16"
    assert value.reranker_device == "cuda"
    for name in ("candidate_limit", "batch_size", "max_length", "timeout_seconds"):
        assert getattr(value, "reranker_" + name) is None


@pytest.mark.parametrize("key,value", [
    ("reranker_provider", "local_qwen3"), ("reranker_provider", "local"),
    ("reranker_provider", "bge"), ("reranker_model", "other/model"),
    ("reranker_device", "auto"), ("reranker_device", "cpu"),
    ("reranker_dtype", "fp32"), ("reranker_dtype", "int8"),
    ("reranker_model_path", ""), ("reranker_candidate_limit", None),
    ("reranker_batch_size", None), ("reranker_max_length", None),
    ("reranker_timeout_seconds", None), ("reranker_max_length", 100000),
])
def test_active_config_rejects_invalid_or_incomplete_profile(key, value):
    domain = import_module("app.retrieval.reranker")
    with pytest.raises(domain.RerankError, match="configuration_invalid"):
        config(**{key: value})


@pytest.mark.parametrize("key,value", [
    ("reranker_candidate_limit", 0), ("reranker_batch_size", -1),
    ("reranker_max_length", 0), ("reranker_timeout_seconds", 0),
    ("reranker_timeout_seconds", float("nan")),
    ("reranker_timeout_seconds", float("inf")),
])
def test_settings_reject_invalid_numeric_values(key, value):
    with pytest.raises(ValidationError):
        settings(**{key: value})


@pytest.mark.parametrize("dtype", ["fp16", "bf16"])
@pytest.mark.parametrize("length", [512, 1024, 2048, 4096])
def test_explicit_supported_dtype_and_length(dtype, length):
    value = config(reranker_dtype=dtype, reranker_max_length=length)
    assert value.dtype == dtype
    assert value.max_length == length


@pytest.mark.parametrize("length", [1, 511, 513, 1536, 2049, 8192])
def test_unapproved_max_lengths_rejected(length):
    domain = import_module("app.retrieval.reranker")
    with pytest.raises(domain.RerankError, match="configuration_invalid"):
        config(reranker_max_length=length)


def test_deprecated_top_k_does_not_change_runtime_configuration():
    assert config(reranker_top_k=-100) == config(reranker_top_k=999)


def test_config_repr_does_not_expose_weight_path():
    value = config(reranker_model_path="D:/private/weights")
    assert "private" not in repr(value)


@pytest.mark.parametrize("key,value", [
    ("dtype", "fp32"), ("device", "cpu"), ("batch_size", 0),
    ("max_length", None), ("timeout_seconds", float("nan")),
])
def test_direct_config_cannot_bypass_validated_profile(key, value):
    d = import_module("app.retrieval.reranker")
    with pytest.raises(d.RerankError, match="configuration_invalid"):
        replace(config(), **{key: value})
