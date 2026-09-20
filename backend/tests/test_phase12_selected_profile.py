"""M5 closure: reject parameter/identity drift without model or dataset evaluation."""
from copy import deepcopy
from importlib import import_module

import pytest

from app.core.config import Settings
from tests.phase12_local.corpus_audit import fingerprint


def module():
    return import_module('tests.phase12_local.selected_profile')


def test_frozen_profile_overrides_legacy_settings_without_mutating_base():
    base = Settings(_env_file=None, reranker_provider='local', reranker_model='legacy')
    before = base.model_dump()
    cfg = module().frozen_settings(base)
    from app.retrieval.reranker import RerankerConfig
    parsed = RerankerConfig.from_settings(cfg)
    assert (parsed.dtype, parsed.device, parsed.max_length, parsed.candidate_limit,
            parsed.batch_size, parsed.timeout_seconds) == ('bf16', 'cuda', 1024, 32, 8, 5.0)
    assert parsed.model == 'BAAI/bge-reranker-v2-m3'
    assert cfg.reranker_provider == 'local_transformers'
    assert cfg.reranker_enabled
    assert base.model_dump() == before
    assert not base.reranker_enabled


@pytest.mark.parametrize('key,value', [
    ('model', 'other'), ('revision', 'other'), ('dtype', 'fp16'), ('device', 'cpu'),
    ('max_length', 512), ('candidate_limit', 8), ('batch_size', 4),
    ('timeout_seconds', 10.0), ('provider', 'other'), ('runtime', 'other'),
    ('local_files_only', False), ('trust_remote_code', True),
])
def test_drift_rejected_even_with_recomputed_payload_digest(key, value):
    data = deepcopy(module().read_selected_profile())
    data['identity'][key] = value
    data['profile_fingerprint'] = fingerprint(data['identity'])
    with pytest.raises(ValueError, match='profile fingerprint'):
        module().validate_selected_profile(data)


def test_model_files_bound_and_category_risk_not_hidden():
    data = module().read_selected_profile()
    assert len(data['identity']['model_files']) == 6
    assert data['known_paraphrase_risk'] == 'accepted known Phase 12 v1 risk'
    assert data['selection_policy'] == 'owner quality-first decision'
    assert data['not_run_by_owner'] == {'bf16-L4096-C8-B1': 'OWNER_STOPPED_NOT_RUN'}
    assert data['final_run_count'] == 0
    assert data['production_enabled'] is False
    data['identity']['model_files'][0]['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='profile fingerprint'):
        module().validate_selected_profile(data)


def test_settings_drift_detected_before_real_work():
    cfg = module().frozen_settings(Settings(_env_file=None))
    module().assert_frozen_settings(cfg)
    with pytest.raises(ValueError, match='settings drift'):
        module().assert_frozen_settings(cfg.model_copy(update={'reranker_candidate_limit': 8}))
