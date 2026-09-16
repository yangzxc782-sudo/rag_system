import copy
import importlib
import pytest

from tests.test_reranker_evaluation import synthetic_reviewed_bundle


def module():
    return importlib.import_module('tests.phase12_local.finalization')


def test_approval_preserves_every_substantive_field():
    m,c,_,_=synthetic_reviewed_bundle()
    before=copy.deepcopy(m)
    frozen=module().approve_and_freeze(m,c,'2026-09-16')
    assert m==before
    assert module().substantive_identity(frozen)==module().substantive_identity(before)
    assert all(q['review_status']=='approved' for q in frozen['queries'])
    assert frozen['reviewer_role']=='project_owner'
    assert frozen['review_reference']=='Phase12 M4 owner approval'
    from tests.phase12_local.evaluation import validate_manifest
    validate_manifest(frozen,c,require_review=True)


def test_no_freeze_on_stale_or_invalid_gold():
    m,c,_,_=synthetic_reviewed_bundle(); m['queries'][0]['qrels'][0]['content_fingerprint']='stale'
    with pytest.raises(ValueError,match='stale'): module().approve_and_freeze(m,c,'2026-09-16')


def test_smoke_disabled_never_creates_service_or_loads_model(monkeypatch):
    monkeypatch.delenv('PHASE12_BGE_PROBE_ENABLED',raising=False)
    from tests.phase12_local.bge_probe import ProbeDisabled
    def forbidden(): raise AssertionError('real service created')
    with pytest.raises(ProbeDisabled):
        module().development_smoke({}, {}, [], {}, forbidden)


def test_smoke_final_input_rejected_before_service(monkeypatch):
    monkeypatch.setenv('PHASE12_BGE_PROBE_ENABLED','1')
    m,c,s,_=synthetic_reviewed_bundle(); s[0]['query_id']='c-definition-0'
    def forbidden(): raise AssertionError('real service created')
    with pytest.raises(ValueError,match='Development'):
        module().development_smoke(m,c,s,{},forbidden)


def test_live_index_missing_vector_rejected():
    c={'chunk_id':'a','document_id':'d','content_fingerprint':'hash','embedding_model':'Qwen3-Embedding-0.6B','embedding_dim':1024,'embedding_status':'embedded'}
    with pytest.raises(ValueError,match='index'):
        module().validate_live_index([c], [{'chunk_id':'a'}])


def test_fixed_smoke_profile_overrides_legacy_env_identity():
    from app.core.config import Settings
    from app.retrieval.reranker import RerankerConfig,MODEL_ID
    base=Settings(_env_file=None,reranker_provider='local',reranker_model='legacy',reranker_model_path='local-fixture')
    cfg=module().smoke_settings(base)
    parsed=RerankerConfig.from_settings(cfg)
    assert parsed.model==MODEL_ID
    assert (parsed.dtype,parsed.candidate_limit,parsed.batch_size,parsed.max_length)==('fp16',8,8,1024)
    assert not base.reranker_enabled
