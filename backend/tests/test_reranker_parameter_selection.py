import copy
import importlib
import pytest

from tests.phase12_local.corpus_audit import fingerprint
from tests.phase12_local.metrics import QUALITY_KEYS


def mod(): return importlib.import_module('tests.phase12_local.parameter_selection')


def good():
    b={k:.6 for k in QUALITY_KEYS}; v={k:.7 for k in QUALITY_KEYS}
    return {'profile_id':'fp16-L1024-C8-B8','profile':{'candidate_limit':8},
            'hardware':{'feasible':True,'incremental_p95_ms':100.,'gpu_peak_mib':4000.,'warm_p95_ms':95.},
            'baseline':b,'variant':v,'categories':{'definition':{'baseline':b.copy(),'variant':v.copy()}}}


@pytest.mark.parametrize('metric',['ndcg_at_8','mrr_at_8','hit_rate_at_1','hit_rate_at_3','recall_at_8'])
def test_quality_rejection(metric):
    r=good(); r['variant'][metric]=.59
    assert mod().quality_gate(r)['status']=='QUALITY_REJECTED'


@pytest.mark.parametrize('metric',['ndcg_at_8','mrr_at_8'])
def test_primary_requires_strict_improvement(metric):
    r=good(); r['variant'][metric]=.6
    assert mod().quality_gate(r)['status']=='QUALITY_REJECTED'


def test_category_degradation_never_auto_winner():
    r=good(); r['categories']['definition']['variant']['recall_at_8']=.59
    gate=mod().quality_gate(r)
    assert gate['status']=='QUALITY_PASS_WITH_CATEGORY_REVIEW'
    assert 'definition' in gate['category_review_required']
    assert mod().select_profile([r])['status']=='AWAITING_PROJECT_OWNER_CATEGORY_REVIEW'


def test_hardware_rejection():
    r=good(); r['hardware']['feasible']=False
    assert mod().quality_gate(r)['status']=='HARDWARE_REJECTED'


@pytest.mark.parametrize('key,value',[('gpu_peak_mib',6501.),('warm_p95_ms',2001.),('incremental_p95_ms',2201.)])
def test_hardware_flag_cannot_override_frozen_slo(key,value):
    r=good(); r['hardware'][key]=value
    assert mod().quality_gate(r)['status']=='HARDWARE_REJECTED'


@pytest.mark.parametrize('field', ['incremental_p95_ms','gpu_peak_mib','warm_p95_ms'])
def test_fixed_cost_order(field):
    a=good(); b=copy.deepcopy(a); b['profile_id']='second'; b['hardware'][field]-=1
    assert mod().select_profile([a,b])['recommended_profile_id']=='second'


def test_smaller_C_tie_and_complete_tie():
    a=good(); b=copy.deepcopy(a); b['profile_id']='second'; a['profile']['candidate_limit']=16
    assert mod().select_profile([a,b])['recommended_profile_id']=='second'
    a['profile']['candidate_limit']=8
    assert mod().select_profile([a,b])['status']=='OWNER_TIE_REVIEW_REQUIRED'


def test_dominance_not_quality_gain_priority():
    a=good(); b=copy.deepcopy(a); b['profile_id']='dominated'
    b['hardware']['incremental_p95_ms']+=10; b['hardware']['gpu_peak_mib']+=10
    b['variant']={k:1. for k in QUALITY_KEYS}
    result=mod().select_profile([b,a])
    assert result['dominated_profile_ids']==['dominated']
    assert result['recommended_profile_id']==a['profile_id']


def test_final_query_rejected_before_retrieval():
    def bad(*args): raise AssertionError('retrieval called')
    with pytest.raises(ValueError,match='FINAL_LEAK'):
        mod().selection_snapshots([{'query_id':'q','split':'final'}],bad,{},'run')


def test_snapshot_freeze_and_no_refresh(tmp_path):
    p=tmp_path/'snapshots.json'
    value={'snapshots':[],'corpus_fingerprint':'c','golden_fingerprint':'g'}
    mod().freeze_snapshot(p,value)
    with pytest.raises(ValueError,match='refresh'): mod().freeze_snapshot(p,value)
    checked=mod().read_snapshot(p,'c','g')
    assert checked['snapshot_fingerprint']==fingerprint(value)
    with pytest.raises(ValueError,match='STALE'): mod().read_snapshot(p,'other','g')
    with pytest.raises(ValueError,match='STALE'): mod().read_snapshot(p,'c','other')


def test_snapshot_digest_drift(tmp_path):
    import json
    p=tmp_path/'s.json'; mod().freeze_snapshot(p,{'snapshots':[],'corpus_fingerprint':'c','golden_fingerprint':'g'})
    data=json.loads(p.read_text()); data['snapshots'].append({}); p.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='fingerprint'): mod().read_snapshot(p,'c','g')


def test_final_profiles_refused():
    r=good(); r['split']='final'
    with pytest.raises(ValueError,match='FINAL_LEAK'): mod().quality_gate(r)


def test_selection_quality_only_cannot_weaken_development_coverage():
    from tests.test_reranker_evaluation import synthetic_reviewed_bundle
    from tests.phase12_local.evaluation import evaluate
    m,c,s,v=synthetic_reviewed_bundle()
    with pytest.raises(ValueError,match='M5 Selection'):
        evaluate(m,c,s,v,split='development',quality_only=True)


def test_selection_prefix_and_derived_same_pool():
    from types import SimpleNamespace
    from tests.phase12_local.corpus_audit import content_hash
    chunks=[{'chunk_id':str(i),'document_id':'d','content_fingerprint':content_hash(str(i))} for i in range(32)]
    c={'corpus_fingerprint':'c','documents':[{'document_id':'d','deletion_status':'normal'}],'chunks':chunks}
    def retrieve(query,capacity):
        return SimpleNamespace(items=[SimpleNamespace(chunk_id=str(i),document_id='d',content=str(i),
            hybrid_score=1.,keyword_rank=i+1,vector_rank=None) for i in range(capacity)])
    result=mod().selection_snapshots([{'query_id':'s','query':'q','split':'selection'}],retrieve,c,'run')
    assert result['prefix_consistent']
    result['snapshots']=[s for s in result['snapshots'] if s['candidate_limit']==32]
    assert len(mod().expanded_snapshots(result,8)[0]['items'])==8


def test_snapshot_creation_rejects_duplicate_candidate():
    from types import SimpleNamespace
    from tests.phase12_local.corpus_audit import content_hash
    c={'corpus_fingerprint':'c','documents':[{'document_id':'d','deletion_status':'normal'}],
       'chunks':[{'chunk_id':'a','document_id':'d','content_fingerprint':content_hash('a')}]}
    def retrieve(*args):
        i=SimpleNamespace(chunk_id='a',document_id='d',content='a',hybrid_score=1.,keyword_rank=1,vector_rank=1)
        return SimpleNamespace(items=[i,i])
    with pytest.raises(ValueError,match='duplicate'):
        mod().selection_snapshots([{'query_id':'s','query':'q','split':'selection'}],retrieve,c,'run')


def test_real_selection_default_gate_zero_load(monkeypatch):
    from tests.phase12_local.bge_probe import ProbeDisabled
    monkeypatch.delenv('PHASE12_BGE_PROBE_ENABLED',raising=False)
    with pytest.raises(ProbeDisabled): mod().run_selection_profile({}, {}, 'missing','never')


def test_stage_b_rejects_profile_without_hardware_pass_before_data_or_load(monkeypatch):
    from tests.phase12_local.m5_hardware_screen import matrix
    monkeypatch.setenv('PHASE12_BGE_PROBE_ENABLED','1')
    p=matrix()[0]
    with pytest.raises(ValueError,match='Stage A feasible'):
        mod().run_selection_profile(p,{'profile':p,'gate':{'feasible':False}},'missing','never')


def test_selection_quality_report_keeps_empty_markdown_slice_null():
    from tests.test_reranker_evaluation import synthetic_reviewed_bundle
    from tests.phase12_local.corpus_audit import content_hash
    from tests.phase12_local.evaluation import evaluate
    m,c,s,_=synthetic_reviewed_bundle()
    prototype=s[0]
    snapshots=[]; variants={}
    for q in m['queries']:
        if q['split']!='selection': continue
        row=copy.deepcopy(prototype)
        row.update(query_id=q['query_id'],query_fingerprint=content_hash(q['query']))
        snapshots.append(row)
        variants[f"{q['query_id']}:C8"]=[dict(chunk_id=i['chunk_id'],original_rank=i['original_rank'],raw_score=1.) for i in row['items']]
    report=evaluate(m,c,snapshots,variants,split='selection',phase='M5',quality_only=True)
    table=report['by_capacity']['8']['variant']['table_formats']
    assert table['markdown']['quality_query_count']==0
    assert table['markdown']['ndcg_at_8'] is None
    assert table['html']['quality_query_count']==5


def test_batch_equivalence_records_order_changes_without_changing_gold():
    a={'profile_id':'a','profile':{'dtype':'fp16','max_length':1024,'candidate_limit':8},
       'raw_rankings':[{'query_id':'q','chunk_id':'one','reranked_rank':1,'raw_score':2.},
                       {'query_id':'q','chunk_id':'two','reranked_rank':2,'raw_score':1.}]}
    b=copy.deepcopy(a); b['profile_id']='b'
    b['raw_rankings'][0].update(reranked_rank=2,raw_score=.5)
    b['raw_rankings'][1].update(reranked_rank=1)
    records=mod().batch_equivalence([a,b])
    assert records[0]['changed_query_ids']==['q']
    assert records[0]['max_score_drift']==1.5


def test_selection_runtime_rejection_overrides_stage_a_and_quality():
    r=good(); r['selection_rejection']={'reason':'gpu_peak_rejected'}
    assert mod().quality_gate(r)['status']=='SELECTION_RUNTIME_REJECTED'
    assert mod().select_profile([r])['status']=='PHASE12_M5_NO_ACCEPTABLE_PROFILE'


def test_selection_memory_check_stops_on_historical_peak():
    from types import SimpleNamespace
    sampler=SimpleNamespace(failed=False,samples=[(0,{'device_used_bytes':6501*2**20})])
    assert mod().selection_memory_rejection(sampler,{'device_used_bytes':4000*2**20})==6501
    sampler.samples=[]
    assert mod().selection_memory_rejection(sampler,{'device_used_bytes':4000*2**20}) is None
    sampler.failed=True
    with pytest.raises(RuntimeError,match='CUDA_UNHEALTHY'):
        mod().selection_memory_rejection(sampler,{'device_used_bytes':4000*2**20})


def test_resume_requires_exact_explicit_evidence_hash(tmp_path):
    import json
    p=tmp_path/'one.json'; r=good()
    p.write_text(json.dumps(r))
    with pytest.raises(ValueError,match='automatic retry'):
        mod().read_completed_selection(p,{},r['profile_id'])
    approved={r['profile_id']:fingerprint(r)}
    assert mod().read_completed_selection(p,approved,r['profile_id'])==r
    r['variant']['mrr_at_8']=0.; p.write_text(json.dumps(r))
    with pytest.raises(ValueError,match='evidence changed'):
        mod().read_completed_selection(p,approved,r['profile_id'])
