"""M7 uses synthetic data here; never load the real held-out manifest."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from tests.phase12_local.corpus_audit import fingerprint, content_hash
from tests.phase12_local.selected_profile import read_selected_profile


def mod():
    from tests.phase12_local import final_evaluation
    return final_evaluation


def identity():
    return {'profile': read_selected_profile(), 'm5_commit': 'm5', 'm6_commit': 'm6',
            'git_commit': 'm6', 'corpus_fingerprint': 'corpus', 'golden_fingerprint': 'golden',
            'final_manifest_fingerprint': 'final', 'evaluation_code_identity': {'a.py': 'hash'},
            'owner_reference': 'synthetic authorization', 'slo': mod().SLO,
            'quality_gate': mod().QUALITY_GATE}


def good_report():
    base = {k: .6 for k in mod().METRICS}
    variant = {k: .7 for k in mod().METRICS}
    return {'by_capacity': {'32': {'baseline': {'overall': base,
        'categories': {c: dict(base) for c in mod().CATEGORIES}},
        'variant': {'overall': variant,
        'categories': {c: dict(variant) for c in mod().CATEGORIES}}}}}


def test_final_lock_created_before_work_and_never_refreshed(tmp_path):
    i = identity(); pre = {'status': 'PASS', 'identity_fingerprint': fingerprint(i), 'final_run_count': 0}
    lock = tmp_path/'lock.json'
    mod().consume_final(lock, i, pre)
    assert json.loads(lock.read_text())['final_run_count'] == 1
    with pytest.raises(FileExistsError):
        mod().consume_final(lock, i, pre)
    # Even infrastructure failure after consumption cannot permit another run.
    try:
        raise OSError('infrastructure')
    except OSError:
        pass
    with pytest.raises(FileExistsError):
        mod().consume_final(lock, i, pre)


@pytest.mark.parametrize('field,value', [('status','FAIL'), ('final_run_count',1), ('identity_fingerprint','drift')])
def test_preflight_rejection_leaves_no_consumed_run(tmp_path, field, value):
    i=identity(); pre={'status':'PASS','identity_fingerprint':fingerprint(i),'final_run_count':0};pre[field]=value
    with pytest.raises(ValueError): mod().consume_final(tmp_path/'lock',i,pre)
    assert not (tmp_path/'lock').exists()


@pytest.mark.parametrize('field,value', [('dtype','fp16'),('device','cpu'),('max_length',512),
    ('candidate_limit',8),('batch_size',4),('timeout_seconds',6),('revision','other'),('provider','other')])
def test_profile_identity_drift_rejected(tmp_path, field, value):
    i=identity();i['profile']['identity'][field]=value
    with pytest.raises(ValueError):mod().consume_final(tmp_path/'lock',i,
        {'status':'PASS','identity_fingerprint':fingerprint(i),'final_run_count':0})


@pytest.mark.parametrize('metric', ['ndcg_at_8','mrr_at_8','hit_rate_at_1','hit_rate_at_3','recall_at_8'])
def test_failed_quality_never_complete(metric):
    r=good_report();r['by_capacity']['32']['variant']['overall'][metric]=.59
    gate=mod().final_quality_gate(r)
    assert not gate['passed']
    assert mod().acceptance(gate, True, True) != 'PHASE12_M7_ACCEPTED'


@pytest.mark.parametrize('metric', ['ndcg_at_8','mrr_at_8'])
def test_primary_tie_fails(metric):
    r=good_report();r['by_capacity']['32']['variant']['overall'][metric]=.6
    assert not mod().final_quality_gate(r)['passed']


def test_selection_risk_acceptance_does_not_approve_final_category():
    r=good_report();r['by_capacity']['32']['variant']['categories']['paraphrase']['recall_at_8']=.5
    q=mod().final_quality_gate(r)
    assert q['passed'] and q['category_regressions']['paraphrase']['recall_at_8']==pytest.approx(-.1)
    assert mod().acceptance(q, True, True)=='AWAITING_PROJECT_OWNER_PHASE12_M7_REVIEW'


@pytest.mark.parametrize('performance,evidence', [(False,True),(True,False)])
def test_missing_real_evidence_or_slo_failure_cannot_complete(performance,evidence):
    assert mod().acceptance(mod().final_quality_gate(good_report()),performance,evidence)!='PHASE12_M7_ACCEPTED'


def test_all_gates_pass_and_no_category_regression():
    assert mod().acceptance(mod().final_quality_gate(good_report()),True,True)=='PHASE12_M7_ACCEPTED'


def test_final_manifest_identity_binds_qrels_and_only_final():
    m={'frozen_manifest_fingerprint':'g','queries':[{'split':'final','query_id':'f','qrels':[1]},
         {'split':'development','query_id':'d'}], 'groups':[{'split':'final','group_id':'f'}]}
    before=mod().final_manifest_identity(m)
    other=deepcopy(m);other['queries'][1]['query_id']='other'
    assert mod().final_manifest_identity(other)==before
    other['queries'][0]['qrels']=[2]
    assert mod().final_manifest_identity(other)!=before


def test_synthetic_final_evaluator_reuses_existing_contract():
    from tests.test_reranker_evaluation import synthetic_reviewed_bundle
    from tests.phase12_local.evaluation import evaluate
    m,c,s,_=synthetic_reviewed_bundle();snapshots=[];scores={}
    for q in m['queries']:
        if q['split']!='final':continue
        row=deepcopy(s[0]);row.update(query_id=q['query_id'],query_fingerprint=content_hash(q['query']),candidate_limit=32)
        snapshots.append(row)
        scores[q['query_id']+':C32']=[dict(chunk_id=i['chunk_id'],original_rank=i['original_rank'],raw_score=float(i['original_rank'])) for i in row['items']]
    result=evaluate(m,c,snapshots,scores,split='final',phase='M7',owner_authorization={
        'phase':'M7','owner_reference':'synthetic unit-only','manifest_fingerprint':m['frozen_manifest_fingerprint']})
    assert result['by_capacity']['32']['baseline']['overall']['quality_query_count']==40
    assert len(result['by_capacity']['32']['variant']['categories'])==9


def test_real_runner_gate_precedes_any_io(monkeypatch):
    monkeypatch.setenv('PHASE12_BGE_PROBE_ENABLED','0')
    operation=Mock()
    monkeypatch.setattr(mod(),'_real',operation)
    from tests.phase12_local.bge_probe import ProbeDisabled
    with pytest.raises(ProbeDisabled):mod().run_final()
    operation.assert_not_called()


@pytest.mark.parametrize('field,value', [('actual_env_unchanged',False),
    ('production_reranker_enabled',True),('worker_closed',False),
    ('identity_unchanged',False)])
def test_cleanup_integrity_failure_revokes_acceptance(field,value):
    report={'quality_gate':mod().final_quality_gate(good_report()),
        'performance':{'passed':True,'gpu_peak_mib':4000},
        'gpu_sampling':{'device_peak_mib':4000},'evidence_complete':True,
        'actual_env_unchanged':True,'production_reranker_enabled':False,
        'worker_closed':True,'identity_unchanged':True,'final_run_count':1}
    report[field]=value
    mod().finalize_report(report)
    assert report['status']=='AWAITING_PROJECT_OWNER_PHASE12_M7_REVIEW'
    assert not report['integrity_passed']


def test_late_gpu_peak_cannot_escape_performance_gate():
    report={'quality_gate':mod().final_quality_gate(good_report()),
        'performance':{'passed':True,'gpu_peak_mib':4000},
        'gpu_sampling':{'device_peak_mib':6600},'evidence_complete':True,
        'actual_env_unchanged':True,'production_reranker_enabled':False,
        'worker_closed':True,'identity_unchanged':True,'final_run_count':1}
    mod().finalize_report(report)
    assert report['status']=='AWAITING_PROJECT_OWNER_PHASE12_M7_REVIEW'
    assert not report['performance']['passed']


def test_incomplete_run_can_be_finalized_without_quality_metrics():
    report={'evidence_complete':False,'final_run_count':1}
    mod().finalize_report(report)
    assert report['status']=='AWAITING_PROJECT_OWNER_PHASE12_M7_REVIEW'


def test_owner_diagnostic_deletions_are_only_allowed_tracked_drift(monkeypatch):
    expected='\n'.join('D\t'+p for p in mod().OWNER_DELETED_DIAGNOSTICS)
    def git(*args):
        return '' if '--cached' in args else expected
    monkeypatch.setattr(mod(),'git',git)
    assert mod().validate_tracked_boundary()==list(mod().OWNER_DELETED_DIAGNOSTICS)
    for change in ('M\tbackend/app/services/rag.py','M\tmanual_test_llm_api.py',
                   expected+'\nD\tbackend/app/main.py'):
        monkeypatch.setattr(mod(),'git',lambda *args, c=change: '' if '--cached' in args else c)
        with pytest.raises(ValueError):mod().validate_tracked_boundary()
