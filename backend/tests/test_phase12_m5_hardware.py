import importlib
import math
import pytest


def mod(): return importlib.import_module('tests.phase12_local.m5_hardware_screen')


def good():
    return {'warm_ms':[100.]*20,'incremental_ms':[110.]*20,'gpu_peak_mib':5000.,
            'scores':[[1.,2.]]*20,'candidate_ids':['a','b'],'failure_reason':None}


def test_matrix_is_explicit_unique_and_excludes_compatibility_lengths():
    matrix=mod().matrix()
    assert len(matrix)==54
    assert len({p['profile_id'] for p in matrix})==54
    assert {p['dtype'] for p in matrix}=={'fp16','bf16'}
    assert {p['max_length'] for p in matrix}=={1024,2048,4096}
    assert {p['candidate_limit'] for p in matrix}=={8,16,32}
    assert all(0<p['batch_size']<=p['candidate_limit'] for p in matrix)
    assert any(p['batch_size']==1 and p['max_length']==4096 for p in matrix)


@pytest.mark.parametrize('field,value,reason',[
    ('failure_reason','oom','oom'),('gpu_peak_mib',6501.,'gpu'),
    ('warm_ms',[2001.]*20,'warm'),('incremental_ms',[2201.]*20,'incremental'),
    ('scores',[[math.nan,2.]]*20,'score'),('scores',[[1.,math.inf]]*20,'score'),
    ('scores',[[1.,2.],[3.,2.]]*10,'ranking'),('warm_ms',[100.],'samples'),
])
def test_rejections(field,value,reason):
    r=good(); r[field]=value
    result=mod().hardware_gate(r)
    assert not result['feasible']
    assert any(reason in why for why in result['reasons'])


def test_feasible_and_boundary():
    r=good(); r['gpu_peak_mib']=6500.; r['warm_ms']=[2000.]*20; r['incremental_ms']=[2200.]*20
    assert mod().hardware_gate(r)['feasible']


def test_disabled_gate_no_real_import(monkeypatch):
    monkeypatch.delenv('PHASE12_BGE_PROBE_ENABLED',raising=False)
    from tests.phase12_local.bge_probe import ProbeDisabled
    with pytest.raises(ProbeDisabled): mod().run_profile({},'never.json')


def test_workload_uses_real_pair_budget_contract_without_gold():
    class Tokenizer:
        def encode(self,text,**kwargs): return list(text)
        def decode(self,ids,**kwargs): return ''.join(ids)
        def num_special_tokens_to_add(self,pair): assert pair; return 4
        def __call__(self,q,p,**kwargs):
            assert kwargs['truncation']=='only_second'
            return {'input_ids':[0]*(len(q)+len(p)+4)}
    profile=mod().matrix()[0]
    bodies,audit=mod().synthetic_workload(Tokenizer(),'query',profile)
    assert len(bodies)==8
    assert all(r['pair_tokens']==1024 for r in audit)
    assert {r['kind'] for r in audit}=={'normal_text','long_text','structured_table'}
    assert len({r['id'] for r in bodies})==8


def test_workload_recounts_joined_text_instead_of_assuming_linear_tokens():
    class Tokenizer:
        def encode(self,text,**kwargs):
            return list(text if len(text)<300 else text[::2])
        def decode(self,ids,**kwargs): return ''.join(ids)
        def num_special_tokens_to_add(self,pair): return 4
        def __call__(self,q,p,**kwargs):
            return {'input_ids':[0]*min(kwargs['max_length'],len(q)+len(p)+4)}
    p=next(p for p in mod().matrix() if p['max_length']==4096)
    bodies,audit=mod().synthetic_workload(Tokenizer(),'query',p)
    assert all(r['pair_tokens']>=.98*4096 for r in audit)


def test_proven_warm_rejection_cannot_advance_to_incremental_or_quality():
    assert mod().warm_gate_passed([2000.]*20)
    assert not mod().warm_gate_passed([2001.]*20)
    assert not mod().warm_gate_passed([100.])
