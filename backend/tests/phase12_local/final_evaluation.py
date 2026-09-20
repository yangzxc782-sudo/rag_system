"""M7 one-shot Final runner. Synthetic tests exercise guards before Final is unlocked.

One Hybrid(C32) per Final query; one BGE scoring of that same pool. No LLM or
external upload, no Selection access, no dataset edits, no parameter selection.
"""
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import subprocess
from threading import Event
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

from tests.phase12_local.corpus_audit import fingerprint, content_hash
from tests.phase12_local.evaluation import CATEGORIES, evaluate, baseline_ranking, rank_scores, validate_manifest
from tests.phase12_local.bge_probe import run_gated, summarize_latencies, gpu_snapshot, sanitize_report
from tests.phase12_local.selected_profile import read_selected_profile, validate_selected_profile, frozen_settings, verify_local_model

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / 'backend/tests/fixtures/phase12'
OUTPUT = ROOT / 'docs/phase-12-m7-results'
M5 = 'f6c1c01200ad859cd7555846a96da4b886333205'
M6 = 'df38c7bd8507601e63c5d9c7db515a4de1823a0b'
GOLDEN_SHA = 'dadaceb59b3437d35f8356399f0dc70c6f60ad52e51433d989a42e418c4f6289'
GOLDEN_FP = 'cdbe63039b4d9a674b2fca3ff7ab1bb3fe941c88c06bec557d165f21b4294c2a'
CORPUS_FP = 'c4e5ffcc12e119e8e3b36d3750ed80e1a0945cc4dfcb400cd8eb5b21762412c1'
METRICS = ('hit_rate_at_1', 'hit_rate_at_3', 'recall_at_8', 'mrr_at_8', 'ndcg_at_8')
SLO = {'warm_p95_ms': 2000, 'incremental_p95_ms': 2200, 'busy_p95_ms': 50,
       'timeout_deadline_ms': 5000, 'timeout_grace_ms': 100, 'gpu_peak_mib': 6500}
QUALITY_GATE = {'primary_strictly_greater': ['ndcg_at_8', 'mrr_at_8'],
                'non_regression': ['hit_rate_at_1', 'hit_rate_at_3', 'recall_at_8'],
                'category_degradation_requires_owner_review': True}
OWNER_REFERENCE = '2026-09-20 Owner: accept M6 with deletion waiver; authorize one M7 Final evaluation'
OWNER_DELETED_DIAGNOSTICS = ('manual_test_llm_api.py', 'manual_test_project_llm_provider.py')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()


def final_manifest_identity(manifest):
    return fingerprint({'golden_fingerprint': manifest['frozen_manifest_fingerprint'],
        'queries': [q for q in manifest['queries'] if q['split'] == 'final'],
        'groups': [g for g in manifest['groups'] if g['split'] == 'final']})


def consume_final(path, identity, preflight):
    validate_selected_profile(identity['profile'])
    if (preflight.get('status') != 'PASS' or preflight.get('final_run_count') != 0
            or preflight.get('identity_fingerprint') != fingerprint(identity)
            or identity.get('slo') != SLO or identity.get('quality_gate') != QUALITY_GATE
            or not identity.get('owner_reference')):
        raise ValueError('Final preflight/identity gate failed')
    # Exclusive creation is the irreversible consumption point, before first Final
    # retrieval. Crash, interruption, partial output or failed Gate never refunds it.
    write_new(path, {'final_run_count': 1, 'status': 'CONSUMED_NO_AUTOMATIC_RETRY',
        'identity_fingerprint': fingerprint(identity), 'identity': identity,
        'consumed_at_utc': datetime.now(timezone.utc).isoformat()})


def final_quality_gate(report):
    arms = report['by_capacity']['32']
    base, variant = arms['baseline']['overall'], arms['variant']['overall']
    if any(type(arm[k]) not in (int,float) or not math.isfinite(arm[k])
           for arm in (base,variant) for k in METRICS):
        raise ValueError('invalid quality metrics')
    primary = {k: variant[k] > base[k] for k in QUALITY_GATE['primary_strictly_greater']}
    nonreg = {k: variant[k] >= base[k] for k in QUALITY_GATE['non_regression']}
    degraded = {}
    for category in CATEGORIES:
        b, v = (arms[name]['categories'][category] for name in ('baseline','variant'))
        changes = {k: v[k]-b[k] for k in METRICS if v[k] is not None and v[k] < b[k]}
        if changes:
            degraded[category] = changes
    return {'passed': all(primary.values()) and all(nonreg.values()),
        'primary': primary, 'non_regression': nonreg,
        'delta': {k: variant[k]-base[k] for k in METRICS},
        'category_regressions': degraded,
        'category_review_status': 'OWNER_REVIEW_REQUIRED' if degraded else 'NO_CATEGORY_DEGRADATION'}


def acceptance(quality, performance_passed, evidence_complete):
    if (quality['passed'] and not quality['category_regressions']
            and performance_passed and evidence_complete):
        return 'PHASE12_M7_ACCEPTED'
    return 'AWAITING_PROJECT_OWNER_PHASE12_M7_REVIEW'


def finalize_report(report):
    """Only decide acceptance after resource cleanup and integrity checks."""
    integrity = (report.get('actual_env_unchanged') is True
        and report.get('production_reranker_enabled') is False
        and report.get('worker_closed') is True
        and report.get('identity_unchanged') is True
        and report.get('final_run_count') == 1)
    report['integrity_passed'] = integrity
    perf = report.get('performance', {})
    peak = max(perf.get('gpu_peak_mib', 0), report.get('gpu_sampling', {}).get('device_peak_mib', 0))
    if perf:
        perf['gpu_peak_mib'] = peak
        perf['passed'] = perf['passed'] and peak <= SLO['gpu_peak_mib']
    report['status'] = acceptance(report.get('quality_gate', {'passed': False}),
        perf.get('passed', False), report.get('evidence_complete', False) and integrity)


def validate_tracked_boundary():
    if git('diff','--cached','--name-only'):
        raise ValueError('staged drift from M6 before Final')
    changes = git('diff','--name-status').splitlines()
    expected = ['D\t'+name for name in OWNER_DELETED_DIAGNOSTICS]
    if changes != expected:
        raise ValueError('tracked drift outside explicit Owner diagnostic deletions')
    return list(OWNER_DELETED_DIAGNOSTICS)


def build_identity():
    if git('branch','--show-current') != 'phase12-bge-reranker' or git('rev-parse','HEAD') != M6:
        raise ValueError('M7 must start from independent M6 commit')
    owner_deletions = validate_tracked_boundary()
    allowed = ('backend/tests/phase12_local/final_evaluation.py', 'backend/tests/test_phase12_m7_final.py',
               'docs/phase-12-m7-results/')
    for name in git('ls-files','--others','--exclude-standard').splitlines():
        if name not in allowed[:2] and not name.startswith(allowed[2]):
            raise ValueError('unexpected untracked work before Final')
    if git('rev-parse', M6+'^') != M5:
        raise ValueError('M5/M6 ancestry drift')
    waiver_path = ROOT/'docs/phase-12-m6-results/owner-waiver-closure-20260920.json'
    waiver = read(waiver_path)
    if (waiver['status'] != 'PHASE12_M6_ACCEPTED_WITH_OWNER_WAIVER'
            or waiver['owner_waiver']['test_result'] != 'NOT_RUN'
            or waiver['owner_waiver']['follow_up'] != 'MANUAL_ACCEPTANCE_DEFERRED'):
        raise ValueError('M6 acceptance missing')
    for name,digest in waiver['evidence_files'].items():
        if sha(waiver_path.parent/name) != digest:
            raise ValueError('M6 evidence drift')
    manifest_path, corpus_path = FIXTURES/'golden_manifest.json', FIXTURES/'corpus_manifest.json'
    if sha(manifest_path) != GOLDEN_SHA:
        raise ValueError('Frozen Golden bytes drift')
    manifest, corpus = read(manifest_path), read(corpus_path)
    validate_manifest(manifest,corpus,require_review=True)
    if manifest['frozen_manifest_fingerprint'] != GOLDEN_FP or corpus['corpus_fingerprint'] != CORPUS_FP:
        raise ValueError('Frozen corpus/Golden identity drift')
    code = list((ROOT/'backend/app').rglob('*.py'))
    code += [Path(__file__), ROOT/'backend/tests/test_phase12_m7_final.py']
    code += list((ROOT/'backend/tests/phase12_local').glob('*.py'))
    profile = read_selected_profile()
    return {'git_commit': M6, 'm5_commit': M5, 'm6_commit': M6,
        'owner_deleted_diagnostics': owner_deletions,
        'owner_boundary_exception': 'Owner confirmed intentional diagnostic deletions and instructed continuing M7; all other tracked files match M6',
        'profile': profile, 'model_revision': profile['identity']['revision'],
        'corpus_fingerprint': CORPUS_FP, 'golden_fingerprint': GOLDEN_FP,
        'golden_file_sha256': GOLDEN_SHA, 'final_manifest_fingerprint': final_manifest_identity(manifest),
        'evaluation_code_identity': {str(p.relative_to(ROOT)).replace('\\','/'): sha(p) for p in sorted(set(code))},
        'slo': SLO, 'quality_gate': QUALITY_GATE, 'owner_reference': OWNER_REFERENCE,
        'm6_owner_waiver': waiver['owner_waiver'], 'm6_waiver_evidence_sha256': sha(waiver_path),
        'historical_runtime_evidence_sha256': sha(ROOT/'docs/phase-12-m3-results/local-smoke.json'),
        'm6_recovery_evidence_sha256': sha(ROOT/'docs/phase-12-m6-results/real-recovery-20260920-02.json')}


def preflight():
    from app.core.config import get_settings
    from tests.phase12_local.finalization import verify_live_corpus
    if (OUTPUT/'final-run-lock.json').exists():
        raise FileExistsError('Final run already consumed')
    identity = build_identity()
    cfg = get_settings()
    if cfg.reranker_enabled:
        raise ValueError('production reranker enabled')
    verify_local_model(frozen_settings(cfg))
    _, live = verify_live_corpus(read(FIXTURES/'corpus_manifest.json'), cfg)
    manifest = read(FIXTURES/'golden_manifest.json')
    queries = [q for q in manifest['queries'] if q['split']=='final']
    result = {'status': 'PASS', 'final_run_count': 0, 'identity_fingerprint': fingerprint(identity),
        'identity': identity, 'live_audit': live,
        'final_query_count':len(queries), 'final_quality_count':sum(q['category']!='robustness' for q in queries),
        'final_robustness_count':sum(q['category']=='robustness' for q in queries),
        'created_at_utc':datetime.now(timezone.utc).isoformat(),
        'actual_env_sha256': {name:sha(ROOT/name) for name in ('.env','backend/.env')},
        'production_enabled':False, 'm7_start_worktree':'clean after M6 commit; bound test harness additions and two subsequently Owner-deleted diagnostics',
        'consumption_rule':'exclusive fixed-path lock before first Final Hybrid; any interruption consumes run'}
    OUTPUT.mkdir(exist_ok=True)
    write_new(OUTPUT/'preflight.json',result)
    return {k:result[k] for k in ('status','final_run_count','identity_fingerprint','final_query_count')}


def fallback_probe(cfg):
    """Real scheduling at frozen 5s deadline, mock blocked provider; no GPU hang."""
    from app.services.reranking import RerankingService
    from app.retrieval.reranker import RerankRequest, RerankCandidate, RerankResult, RerankScore
    release = Event(); calls=[]
    def blocked(req, *, budget):
        calls.append(req.request_id)
        if not release.wait(15):
            raise RuntimeError('mock provider safety release')
        return RerankResult(req.request_id,tuple(RerankScore(c.chunk_id,c.original_rank,0.) for c in req.candidates))
    fake=SimpleNamespace(rerank=blocked,close=lambda:None)
    svc=RerankingService(cfg,provider_factory=lambda _:fake)
    request=RerankRequest('synthetic-timeout','synthetic only',(RerankCandidate('synthetic',1,'synthetic only'),))
    try:
        start=perf_counter();result=svc.rerank(request);elapsed=(perf_counter()-start)*1000
        assert result.failure_reason=='timeout'
        busy=[]
        for _ in range(20):
            start=perf_counter();b=svc.rerank(request);busy.append((perf_counter()-start)*1000)
            assert b.failure_reason=='busy'
        stats=summarize_latencies(busy)
        assert len(calls)==1
        assert elapsed<=5100 and stats['p95_ms']<=50, 'fallback SLO failure'
        return {'mode':'real RerankingService with fault-injected mock provider; no real CUDA hang',
            'profile_fingerprint':read_selected_profile()['profile_fingerprint'],
            'timeout_deadline_ms':5000,'timeout_return_ms':elapsed,'busy':stats,'provider_call_count':len(calls),'passed':True}
    finally:
        release.set();svc.close()


def capture_snapshot(query, result, corpus, run_id):
    snapshot={'query_id':query['query_id'],'query_fingerprint':content_hash(query['query']),
        'candidate_limit':32,'retrieval_run_id':run_id,'corpus_fingerprint':corpus['corpus_fingerprint'],
        'items':[{'chunk_id':i.chunk_id,'document_id':i.document_id,'original_rank':n,
            'hybrid_score':i.hybrid_score,'keyword_rank':i.keyword_rank,'vector_rank':i.vector_rank,
            'content_fingerprint':content_hash(i.content)} for n,i in enumerate(result.items,1)]}
    baseline_ranking(snapshot,corpus)
    return snapshot


def run_final():
    return run_gated(_real)


def _real():
    if (OUTPUT/'final-run-lock.json').exists():
        raise FileExistsError('Final consumed; Owner decision required, never automatic retry')
    pre=read(OUTPUT/'preflight.json');identity=build_identity()
    if pre['identity_fingerprint']!=fingerprint(identity):
        raise ValueError('preflight identity drift')
    if not all(sha(ROOT/p)==digest for p,digest in pre['actual_env_sha256'].items()):
        raise ValueError('actual env drift since preflight')
    os.environ['HF_HUB_OFFLINE']=os.environ['TRANSFORMERS_OFFLINE']='1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS']='1'
    logging.disable(logging.CRITICAL)
    from app.core.config import get_settings
    from app.retrieval.embeddings import get_embedding_provider
    from app.retrieval.local_cross_encoder import LocalCrossEncoderProvider, _load_local_model
    from app.retrieval.reranker import RerankerConfig, RerankRequest, RerankCandidate
    from app.services import hybrid_search, rag, reranking
    from app.search_engine.client import get_search_engine_client
    from tests.phase12_local.finalization import verify_live_corpus, read_only_session
    from tests.phase12_local.bge_smoke import DeviceSampler, Observation
    import torch
    base=get_settings();cfg=frozen_settings(base)
    if base.reranker_enabled:raise ValueError('production must remain disabled')
    verify_local_model(cfg)
    corpus=read(FIXTURES/'corpus_manifest.json');manifest=read(FIXTURES/'golden_manifest.json')
    _,live=verify_live_corpus(corpus,cfg)
    queries=[q for q in manifest['queries'] if q['split']=='final']
    report={'status':'PREFLIGHT_RUNTIME','final_run_count':0,'identity_fingerprint':fingerprint(identity),
        'live_audit_before':live,'requests':[],'snapshots':[],'scores':{},'model_loads':[],
        'final_queries_persisted':False,'m6_owner_waiver':identity['m6_owner_waiver'],
        'latency_method':'same Hybrid(C32) retrieved once; off/on reranking of identical snapshot; no repeated Final scoring',
        'started_at_utc':datetime.now(timezone.utc).isoformat()}
    service=provider=client=sampler=None
    def save():
        (OUTPUT/'evaluation.json').write_text(json.dumps(sanitize_report(report),ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    try:
        report['fallback_probe']=fallback_probe(cfg)
        assert torch.cuda.is_available() and torch.cuda.is_bf16_supported()
        embedding=get_embedding_provider(cfg)
        embedding.encode_query('synthetic readiness check')
        retained_model=id(embedding._model)
        client=get_search_engine_client(cfg)
        torch.cuda.reset_peak_memory_stats()
        sampler=DeviceSampler(torch);sampler.thread.start()
        observer=Observation(torch,_load_local_model)
        provider=LocalCrossEncoderProvider(RerankerConfig.from_settings(cfg),model_loader=observer.load)
        start=perf_counter();provider._ensure_loaded();report['cold_load_ms']=(perf_counter()-start)*1000
        assert observer.loads[0]['parameter_dtype']=='torch.bfloat16'
        service=reranking.RerankingService(cfg,provider_factory=lambda _:provider)
        readiness=service.rerank(RerankRequest('synthetic-readiness','synthetic readiness check',
            (RerankCandidate('synthetic-readiness',1,'Only a synthetic model readiness passage.'),)))
        assert readiness.failure_reason is None, 'synthetic readiness failed before consuming Final'
        report['readiness_bge_forwards']=len(observer.forwards)
        sampler.check()
        # Recheck all immutable sources after startup, before consuming the one run.
        if build_identity()!=identity:raise ValueError('identity drift during runtime startup')
        consume_final(OUTPUT/'final-run-lock.json',identity,pre)
        report.update(status='FINAL_RUNNING',final_run_count=1)
        save()
        run_id='phase12-m7-'+fingerprint(identity)[:20]
        with patch.object(reranking,'get_reranking_service',return_value=service):
            for q in queries:
                row={'query_id':q['query_id'],'category':q['category'],'status':'RUNNING','hybrid_call_count':0}
                report['requests'].append(row);save()
                start=perf_counter()
                result=hybrid_search.hybrid_search_chunks(None,query=q['query'],limit=32,settings=cfg,
                    client=client,embedding_provider=embedding,deletion_filter_session_factory=read_only_session)
                row.update(hybrid_ms=(perf_counter()-start)*1000,hybrid_call_count=1)
                snapshot=capture_snapshot(q,result,corpus,run_id);report['snapshots'].append(snapshot)
                start=perf_counter()
                off=rag.optional_rerank_chunks(q['query'],result,cfg.model_copy(update={'reranker_enabled':False}),limit=8)
                off_ms=(perf_counter()-start)*1000
                captured=[];original_rerank=service.rerank
                def scoring(req):
                    t=perf_counter();scored=original_rerank(req)
                    row['warm_rerank_ms']=(perf_counter()-t)*1000
                    captured.append(scored)
                    return scored
                before=len(observer.forwards);torch.cuda.synchronize();start=perf_counter()
                with patch.object(service,'rerank',side_effect=scoring):
                    on=rag.optional_rerank_chunks(q['query'],result,cfg,limit=8)
                torch.cuda.synchronize();on_ms=(perf_counter()-start)*1000
                row.update(incremental_ms=on_ms-off_ms,rerank_on_ms=on_ms,rerank_off_ms=off_ms,
                    retrieval_baseline_ms=row['hybrid_ms']+off_ms,retrieval_on_ms=row['hybrid_ms']+on_ms,
                    candidate_count=len(result.items),bge_forward_count=len(observer.forwards)-before,
                    fallback_reason=on.fallback_reason,gpu=gpu_snapshot(torch))
                if not result.items:
                    assert not captured and on.fallback_reason=='no_candidates'
                    rows=[];row['warm_rerank_ms']=0.0
                else:
                    assert on.applied and len(captured)==1 and captured[0].failure_reason is None, 'Final reranker failure'
                    rows=[{'chunk_id':s.chunk_id,'original_rank':s.original_rank,'raw_score':s.raw_score} for s in captured[0].scores]
                ranking=rank_scores(snapshot,rows)
                assert [i.chunk_id for i in on.search_result.items]==ranking[:8]
                assert off.search_result.items==result.items[:8]
                row.update(status='PASS',baseline_top8=[i.chunk_id for i in off.search_result.items],variant_top8=ranking[:8])
                report['scores'][q['query_id']+':C32']=rows
                sampler.check();assert id(embedding._model)==retained_model
                save()
                print(json.dumps({'final_queries_completed':len(report['scores']),'final_run_count':1}),flush=True)
        report['evaluation']=evaluate(manifest,corpus,report['snapshots'],report['scores'],split='final',phase='M7',
            owner_authorization={'phase':'M7','owner_reference':OWNER_REFERENCE,'manifest_fingerprint':GOLDEN_FP})
        report['quality_gate']=final_quality_gate(report['evaluation'])
        report['performance']={
            'warm':summarize_latencies([r['warm_rerank_ms'] for r in report['requests'] if r['candidate_count']]),
            'incremental':summarize_latencies([max(0,r['incremental_ms']) for r in report['requests']]),
            'retrieval_baseline':summarize_latencies([r['retrieval_baseline_ms'] for r in report['requests']]),
            'retrieval_on':summarize_latencies([r['retrieval_on_ms'] for r in report['requests']]),
            'gpu_peak_mib':max(s[1]['device_used_bytes'] for s in sampler.samples)/2**20}
        perf=report['performance']
        perf['passed']=(perf['warm']['p95_ms']<=2000 and perf['incremental']['p95_ms']<=2200
                        and perf['gpu_peak_mib']<=6500 and report['fallback_probe']['passed'])
        _,report['live_audit_after']=verify_live_corpus(corpus,cfg)
        assert sha(FIXTURES/'golden_manifest.json')==GOLDEN_SHA
        verify_local_model(cfg)
        report.update(evidence_complete=True,embedding_retained=True)
    except Exception as exc:
        report.update(status='AWAITING_PROJECT_OWNER_PHASE12_M7_REVIEW',error_type=type(exc).__name__,
            evidence_complete=False,final_run_count=int((OUTPUT/'final-run-lock.json').exists()))
        for row in report['requests']:
            if row['status']=='RUNNING':row['status']='STOP'
        # No exception string / query / prompt / provider credentials in evidence.
    finally:
        if sampler is not None:
            sampler.close()
            report['gpu_sampling']={'sample_count':len(sampler.samples),
                'max_gap_ms':max((1000*(b[0]-a[0]) for a,b in zip(sampler.samples,sampler.samples[1:])),default=0),
                'device_peak_mib':max((s[1]['device_used_bytes'] for s in sampler.samples),default=0)/2**20}
        if service is not None:
            service.close();report['worker_closed']=service.wait_closed(0)
        elif provider is not None:provider.close()
        if client is not None:client.close()
        if 'observer' in locals():
            report['model_loads']=observer.loads
            report['bge_total_forwards']=len(observer.forwards)
        report['actual_env_unchanged']=all(sha(ROOT/p)==digest for p,digest in pre['actual_env_sha256'].items())
        report['production_reranker_enabled']=get_settings().reranker_enabled
        try:
            report['identity_unchanged']=build_identity()==identity
        except Exception:
            report['identity_unchanged']=False
        finalize_report(report)
        report['finished_at_utc']=datetime.now(timezone.utc).isoformat()
        save()
    return {'status':report['status'],'final_run_count':report['final_run_count'],
        'completed_queries':len(report['scores']),'quality_gate':report.get('quality_gate'),
        'performance':report.get('performance'),'error_type':report.get('error_type')}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--action',choices=['preflight','run'],required=True)
    args=parser.parse_args()
    print(json.dumps(preflight() if args.action=='preflight' else run_final(),ensure_ascii=False))
