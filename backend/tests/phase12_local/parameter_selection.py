"""M5 test-only Selection protocol and deterministic owner-frozen decision rules.

No model or CUDA imports in the metric/selection core. Final is never admitted.
"""
from copy import deepcopy
import json
import math
from pathlib import Path

from tests.phase12_local.corpus_audit import fingerprint
from tests.phase12_local.metrics import QUALITY_KEYS

PRIMARY=('ndcg_at_8','mrr_at_8')
NON_REGRESSION=('hit_rate_at_1','hit_rate_at_3','recall_at_8')
QUALITY=PRIMARY+NON_REGRESSION


def reject_final(rows):
    if any(r.get('split')=='final' or str(r.get('query_id','')).startswith('p12-fin-') for r in rows):
        raise ValueError('PHASE12_M5_FINAL_LEAK')


def quality_gate(row):
    reject_final([row])
    if row.get('selection_rejection'):
        return {'status':'SELECTION_RUNTIME_REJECTED','category_review_required':[],
                'reason':row['selection_rejection']['reason']}
    h=row['hardware']
    if not h['feasible'] or any(type(h.get(k)) not in (int,float) or not math.isfinite(h[k]) or h[k]>ceiling
            for k,ceiling in (('warm_p95_ms',2000),('incremental_p95_ms',2200),('gpu_peak_mib',6500))):
        return {'status':'HARDWARE_REJECTED','category_review_required':[]}
    baseline,variant=row['baseline'],row['variant']
    primary={k:variant[k]>baseline[k] for k in PRIMARY}
    nonreg={k:variant[k]>=baseline[k] for k in NON_REGRESSION}
    degraded={cat:[k for k in QUALITY if r['variant'][k] is not None and r['variant'][k]<r['baseline'][k]]
              for cat,r in row['categories'].items()}
    degraded={cat:keys for cat,keys in degraded.items() if keys}
    passed=all(primary.values()) and all(nonreg.values())
    return {'status':('QUALITY_PASS_WITH_CATEGORY_REVIEW' if degraded else 'QUALITY_PASS') if passed else 'QUALITY_REJECTED',
            'primary':primary,'non_regression':nonreg,'category_review_required':degraded,
            'category_status':'CATEGORY_REVIEW_REQUIRED' if degraded else 'NO_CATEGORY_DEGRADATION'}


def select_profile(rows):
    checked=[dict(r,gate=quality_gate(r)) for r in rows]
    accepted=[r for r in checked if r['gate']['status']=='QUALITY_PASS']
    pending=[r['profile_id'] for r in checked if r['gate']['status']=='QUALITY_PASS_WITH_CATEGORY_REVIEW']
    if not accepted:
        return {'status':'AWAITING_PROJECT_OWNER_CATEGORY_REVIEW' if pending else 'PHASE12_M5_NO_ACCEPTABLE_PROFILE',
                'category_review_profile_ids':pending,'gates':{r['profile_id']:r['gate'] for r in checked}}
    def dominates(a,b):
        x,y=a['hardware'],b['hardware']
        keys=('incremental_p95_ms','gpu_peak_mib')
        return all(x[k]<=y[k] for k in keys) and any(x[k]<y[k] for k in keys)
    dominated=[r['profile_id'] for r in accepted if any(dominates(s,r) for s in accepted if s is not r)]
    def cost(r):
        h=r['hardware']
        return h['incremental_p95_ms'],h['gpu_peak_mib'],h['warm_p95_ms'],r['profile']['candidate_limit']
    survivors=sorted((r for r in accepted if r['profile_id'] not in dominated),key=cost)
    tied=len(survivors)>1 and cost(survivors[0])==cost(survivors[1])
    return {'status':'OWNER_TIE_REVIEW_REQUIRED' if tied else 'RECOMMENDED_PHASE12_PROFILE',
            'recommended_profile_id':None if tied else survivors[0]['profile_id'],
            'dominated_profile_ids':dominated,'cost_order':[r['profile_id'] for r in survivors],
            'category_review_profile_ids':pending,'gates':{r['profile_id']:r['gate'] for r in checked}}


def selection_snapshots(queries,retrieve,corpus,run_id):
    reject_final(queries)
    if not queries or any(q['split']!='selection' for q in queries):
        raise ValueError('Selection only')
    from tests.phase12_local.candidate_snapshots import _capture_prefix
    return _capture_prefix(queries,retrieve,corpus,run_id)


def freeze_snapshot(path,value):
    path=Path(path)
    if path.exists(): raise ValueError('snapshot refresh forbidden')
    reject_final(value.get('snapshots',[]))
    result=deepcopy(value)
    if 'snapshot_fingerprint' in result: raise ValueError('already signed snapshot')
    result['snapshot_fingerprint']=fingerprint(result)
    with path.open('x',encoding='utf-8') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2,allow_nan=False); stream.write('\n')
    return result


def read_snapshot(path,corpus_fingerprint,golden_fingerprint):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    unsigned={k:v for k,v in data.items() if k!='snapshot_fingerprint'}
    if fingerprint(unsigned)!=data['snapshot_fingerprint']: raise ValueError('snapshot fingerprint drift')
    if data['corpus_fingerprint']!=corpus_fingerprint or data['golden_fingerprint']!=golden_fingerprint:
        raise ValueError('PHASE12_M5_DATASET_STALE')
    reject_final(data['snapshots'])
    return data


def expanded_snapshots(bundle,capacity):
    reject_final(bundle['snapshots'])
    if bundle['strategy']=='one_C32_snapshot_prefixes':
        return [dict(s,candidate_limit=capacity,items=s['items'][:capacity]) for s in bundle['snapshots']]
    return [s for s in bundle['snapshots'] if s['candidate_limit']==capacity]


def batch_equivalence(rows):
    groups={}; records=[]
    for row in rows:
        reject_final([row])
        p=row['profile']; groups.setdefault((p['dtype'],p['max_length'],p['candidate_limit']),[]).append(row)
    def by_query(row):
        queries={}
        for r in row['raw_rankings']: queries.setdefault(r['query_id'],[]).append(r)
        return {q:sorted(rs,key=lambda r:r['reranked_rank']) for q,rs in queries.items()}
    for profiles in groups.values():
        reference=profiles[0]; first=by_query(reference)
        for other in profiles[1:]:
            second=by_query(other); changed=[]; drift=0.
            if set(first)!=set(second): raise ValueError('batch query identity mismatch')
            for q,a in first.items():
                b=second[q]
                from tests.phase12_local.evaluation import same_pool
                same_pool([r['chunk_id'] for r in a],[r['chunk_id'] for r in b])
                if [r['chunk_id'] for r in a]!=[r['chunk_id'] for r in b]: changed.append(q)
                lookup={r['chunk_id']:r['raw_score'] for r in a}
                drift=max(drift,max(abs(r['raw_score']-lookup[r['chunk_id']]) for r in b))
            records.append({'reference_profile_id':reference['profile_id'],'profile_id':other['profile_id'],
                            'changed_query_ids':changed,'max_score_drift':drift})
    return records


def selection_memory_rejection(sampler,current):
    if sampler.failed: raise RuntimeError('PHASE12_M5_CUDA_UNHEALTHY')
    peak=max([current['device_used_bytes']]+[s['device_used_bytes'] for _,s in sampler.samples])/2**20
    return peak if peak>6500 else None


def read_completed_selection(path,approved,profile_id):
    if profile_id not in approved:
        raise ValueError('Selection scoring already exists; no automatic retry')
    data=json.loads(Path(path).read_text())
    if data['profile_id']!=profile_id or fingerprint(data)!=approved[profile_id]:
        raise ValueError('completed Selection evidence changed')
    return data


class SelectionRuntimeRejected(Exception):
    def __init__(self,reason,**evidence):
        self.evidence=dict(reason=reason,**evidence)
        super().__init__(reason)


def capture_selection(output):
    """Owner-authorized read-only Hybrid capture; does not load any BGE model."""
    import os
    from app.core.config import get_settings
    from app.retrieval.embeddings import get_embedding_provider
    from app.services.hybrid_search import hybrid_search_chunks
    from app.search_engine.client import get_search_engine_client
    from tests.phase12_local.evaluation import validate_manifest
    from tests.phase12_local.finalization import verify_live_corpus,read_only_session
    if Path(output).exists(): raise ValueError('snapshot refresh forbidden')
    os.environ['HF_HUB_OFFLINE']=os.environ['TRANSFORMERS_OFFLINE']='1'
    p=Path(__file__).resolve().parents[2]/'tests/fixtures/phase12'
    corpus=json.loads((p/'corpus_manifest.json').read_text(encoding='utf-8'))
    manifest=json.loads((p/'golden_manifest.json').read_text(encoding='utf-8'))
    # Whole-file integrity is verified without evaluating or displaying Final.
    validate_manifest(manifest,corpus,require_review=True)
    cfg=get_settings(); _,before=verify_live_corpus(corpus,cfg)
    embedding=get_embedding_provider(cfg); client=get_search_engine_client(cfg)
    try:
        calls=0
        def retrieve(query,limit):
            nonlocal calls
            r=hybrid_search_chunks(None,query=query,limit=limit,settings=cfg,client=client,
                embedding_provider=embedding,deletion_filter_session_factory=read_only_session)
            calls+=1
            if calls%3==0: print(json.dumps({'selection_prefix_completed':calls//3}),flush=True)
            return r
        queries=[q for q in manifest['queries'] if q['split']=='selection' and q['category']!='robustness']
        result=selection_snapshots(queries,retrieve,corpus,'m5-selection-prefix-20260916')
        result.pop('snapshot_fingerprint')
        result['purpose']='M5 frozen Selection same-pool comparison; no Final retrieval'
        if result['prefix_consistent']:
            result['snapshots']=[s for s in result['snapshots'] if s['candidate_limit']==32]
        _,after=verify_live_corpus(corpus,cfg)
        result.update(golden_fingerprint=manifest['frozen_manifest_fingerprint'],
                      live_audit_before=before,live_audit_after=after,final_run_count=0,
                      hybrid_configuration={k:getattr(cfg,k) for k in ('hybrid_keyword_top_k','hybrid_vector_top_k','hybrid_rrf_k','hybrid_keyword_weight','hybrid_vector_weight')})
        return freeze_snapshot(output,result)
    finally: client.close()


def run_selection_profile(profile,hardware,bundle_path,output):
    from tests.phase12_local.bge_probe import run_gated
    return run_gated(lambda:_real_selection(profile,hardware,bundle_path,output))


def _real_selection(profile,hardware,bundle_path,output):
    import hashlib
    import os
    import subprocess
    from time import perf_counter
    from app.core.config import get_settings
    from app.retrieval.embeddings import get_embedding_provider
    from app.retrieval.local_cross_encoder import LocalCrossEncoderProvider
    from app.retrieval.reranker import RerankerConfig,RerankCandidate,RerankRequest
    from app.services.reranking import RerankingService
    from tests.phase12_local.finalization import verify_live_corpus,smoke_settings
    from tests.phase12_local.evaluation import evaluate,rank_scores,validate_manifest
    from tests.phase12_local.bge_probe import score_stability,gpu_snapshot,summarize_latencies
    from tests.phase12_local.bge_smoke import DeviceSampler
    from tests.phase12_local.m5_hardware_screen import matrix,hardware_gate
    if profile not in matrix() or hardware['profile']!=profile or not hardware['gate']['feasible']:
        raise ValueError('Stage B requires exact Stage A feasible profile')
    if hardware['gate']!=hardware_gate(hardware):
        raise ValueError('Stage A evidence disagrees with gate')
    if Path(output).exists(): raise ValueError('Selection result overwrite forbidden')
    os.environ['HF_HUB_OFFLINE']=os.environ['TRANSFORMERS_OFFLINE']='1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS']='1'
    import torch
    root=Path(__file__).resolve().parents[2]
    p=root/'tests/fixtures/phase12'
    corpus=json.loads((p/'corpus_manifest.json').read_text(encoding='utf-8'))
    manifest=json.loads((p/'golden_manifest.json').read_text(encoding='utf-8'))
    validate_manifest(manifest,corpus,require_review=True)
    bundle=read_snapshot(bundle_path,corpus['corpus_fingerprint'],manifest['frozen_manifest_fingerprint'])
    snapshots=expanded_snapshots(bundle,profile['candidate_limit'])
    queries={q['query_id']:q for q in manifest['queries'] if q['split']=='selection' and q['category']!='robustness'}
    reject_final(snapshots)
    if {s['query_id'] for s in snapshots}!=set(queries): raise ValueError('Selection snapshot coverage')
    cfg=smoke_settings(get_settings()).model_copy(update=dict(reranker_dtype=profile['dtype'],
        reranker_max_length=profile['max_length'],reranker_candidate_limit=profile['candidate_limit'],
        reranker_batch_size=profile['batch_size']))
    contents,audit=verify_live_corpus(corpus,cfg)
    identity=json.loads((root.parent/'docs/phase-12-m0-results/model-identity.json').read_text())
    for entry in identity['files']:
        with (Path(cfg.reranker_model_path)/entry['name']).open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=entry['sha256']: raise ValueError('model drift')
    embedding=get_embedding_provider(cfg); embedding.encode_query('合成共存验证，不使用评测标签。')
    held_model_id=id(embedding._model)
    provider=LocalCrossEncoderProvider(RerankerConfig.from_settings(cfg)); provider._ensure_loaded()
    service=RerankingService(cfg,provider_factory=lambda _:provider)
    sampler=DeviceSampler(torch); sampler.thread.start()
    variants={}; raw=[]; timings=[]; token_rows=[]; repeat_stability=[]
    provenance=dict(profile_id=profile['profile_id'],profile=profile,split='selection',hardware=hardware['gate'],
        corpus_fingerprint=corpus['corpus_fingerprint'],golden_fingerprint=manifest['frozen_manifest_fingerprint'],
        selection_snapshot_fingerprint=bundle['snapshot_fingerprint'],model_revision=identity['revision'],
        final_run_count=0,code_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip())
    def check_memory():
        peak=selection_memory_rejection(sampler,gpu_snapshot(torch))
        if peak is not None: raise SelectionRuntimeRejected('gpu_peak_rejected',gpu_peak_mib=peak)
    try:
        check_memory()
        for snapshot in snapshots:
            q=queries[snapshot['query_id']]
            candidates=tuple(RerankCandidate(i['chunk_id'],i['original_rank'],contents[i['chunk_id']]) for i in snapshot['items'])
            request=RerankRequest(q['query_id'],q['query'],candidates)
            runs=[]; scores=None
            for repeat in range(2):
                torch.cuda.synchronize(); start=perf_counter(); result=service.rerank(request)
                elapsed=(perf_counter()-start)*1000
                if result.failure_reason:
                    if result.failure_reason in ('oom','timeout'):
                        raise SelectionRuntimeRejected(result.failure_reason,return_latency_ms=elapsed,
                                                       query_id=q['query_id'])
                    raise RuntimeError(f'Selection stopped: {result.failure_reason}; query_id={q["query_id"]}')
                torch.cuda.synchronize()
                check_memory()
                scores=[dict(chunk_id=s.chunk_id,original_rank=s.original_rank,raw_score=s.raw_score) for s in result.scores]
                rank_scores(snapshot,scores)
                mapping={s['chunk_id']:s['raw_score'] for s in scores}
                runs.append([mapping[c.chunk_id] for c in candidates])
                timings.append({'query_id':q['query_id'],'repeat':repeat,'wall_ms':elapsed})
                if repeat==0:
                    variants[f'{q["query_id"]}:C{profile["candidate_limit"]}']=scores
                    grade={r['chunk_id']:r['grade'] for r in q['qrels']}
                    raw.extend(dict(query_id=q['query_id'],reranked_rank=i,grade=grade.get(s['chunk_id'],0),**s) for i,s in enumerate(scores,1))
            stability=score_stability(runs,[c.chunk_id for c in candidates])
            if not stability['ranking_stable']: raise RuntimeError('Selection repeated ranking unstable')
            repeat_stability.append(dict(query_id=q['query_id'],max_score_drift=stability['max_score_drift'],ranking_stable=True))
            tokenizer=provider._loaded.tokenizer
            qlen=len(tokenizer.encode(q['query'],add_special_tokens=False))
            passage_budget=profile['max_length']-qlen-tokenizer.num_special_tokens_to_add(pair=True)
            for candidate in candidates:
                n=len(tokenizer.encode(candidate.content,add_special_tokens=False,truncation=False))
                token_rows.append(dict(query_id=q['query_id'],chunk_id=candidate.chunk_id,
                    passage_tokens=n,passage_budget=passage_budget,truncated=n>passage_budget,
                    truncation_ratio=max(0.,1-passage_budget/n) if n else 0.))
        report=evaluate(manifest,corpus,snapshots,variants,split='selection',phase='M5',quality_only=True)
        if id(embedding._model)!=held_model_id: raise RuntimeError('embedding residency changed')
        sampler.close()
        if sampler.failed: raise RuntimeError('PHASE12_M5_CUDA_UNHEALTHY')
        peak=max(v['device_used_bytes'] for _,v in sampler.samples)/2**20
        if peak>6500: raise SelectionRuntimeRejected('gpu_peak_rejected',gpu_peak_mib=peak)
        latency=summarize_latencies([t['wall_ms'] for t in timings])
        if latency['p95_ms']>2000:
            raise SelectionRuntimeRejected('selection_warm_latency_rejected',warm_p95_ms=latency['p95_ms'])
        _,after=verify_live_corpus(corpus,cfg)
        capacity=report['by_capacity'][str(profile['candidate_limit'])]
        row=dict(profile_id=profile['profile_id'],profile=profile,split='selection',hardware=hardware['gate'],
            baseline=capacity['baseline']['overall'],variant=capacity['variant']['overall'],
            categories={cat:{name:capacity[name]['categories'][cat] for name in ('baseline','variant')}
                        for cat in capacity['baseline']['categories']},
            report=report,raw_rankings=raw,repeat_stability=repeat_stability,timings=timings,
            truncation=token_rows,selection_latency=summarize_latencies([t['wall_ms'] for t in timings]),
            gpu_peak_mib=peak,gpu=gpu_snapshot(torch),embedding_retained=True,
            corpus_fingerprint=corpus['corpus_fingerprint'],golden_fingerprint=manifest['frozen_manifest_fingerprint'],
            selection_snapshot_fingerprint=bundle['snapshot_fingerprint'],model_revision=identity['revision'],
            runtime=hardware['runtime'],model_id=identity['model_id'],device='cuda',
            parameter_dtype=str(next(provider._loaded.model.parameters()).dtype),
            live_audit_before=audit,live_audit_after=after,final_run_count=0,
            code_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
            production_validator_sha256=hashlib.sha256((root/'app/retrieval/reranker.py').read_bytes()).hexdigest())
        row['worktree_identity']=fingerprint({name:hashlib.sha256((root/name).read_bytes()).hexdigest()
            for name in ('app/retrieval/reranker.py','app/retrieval/local_cross_encoder.py',
                         'app/services/reranking.py','app/services/rag.py','app/services/hybrid_search.py',
                         'tests/phase12_local/parameter_selection.py','tests/phase12_local/evaluation.py')})
        row['gate']=quality_gate(row)
        Path(output).write_text(json.dumps(row,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        return {'profile_id':profile['profile_id'],'gate':row['gate'],'variant':row['variant']}
    except SelectionRuntimeRejected as exc:
        # Preserve the failed experiment; no altered-profile retry or partial metrics.
        service.wait_idle(60)
        if provider._unavailable: raise RuntimeError('PHASE12_M5_CUDA_UNHEALTHY')
        sampler.close()
        if sampler.failed: raise RuntimeError('PHASE12_M5_CUDA_UNHEALTHY')
        _,after=verify_live_corpus(corpus,cfg)
        row=dict(provenance,selection_rejection=exc.evidence,completed_requests=len(timings),
            completed_query_pairs=len(repeat_stability),quality_metrics_available=False,
            gpu=gpu_snapshot(torch),live_audit_before=audit,live_audit_after=after,
            embedding_retained=id(embedding._model)==held_model_id)
        row['gate']=quality_gate(row)
        with Path(output).open('x',encoding='utf-8') as stream:
            json.dump(row,stream,ensure_ascii=False,indent=2,allow_nan=False); stream.write('\n')
        return {'profile_id':profile['profile_id'],'gate':row['gate']}
    finally:
        sampler.close(); service.close()


def freeze_hardware_feasibility(directory):
    from tests.phase12_local.m5_hardware_screen import matrix,hardware_gate
    directory=Path(directory)
    declaration=json.loads((directory/'matrix.json').read_text())
    unsigned={k:v for k,v in declaration.items() if k!='matrix_fingerprint'}
    if fingerprint(unsigned)!=declaration['matrix_fingerprint'] or declaration['profiles']!=matrix():
        raise ValueError('matrix drift')
    records=[]
    for profile in declaration['profiles']:
        data=json.loads((directory/(profile['profile_id']+'.json')).read_text())
        if data['profile']!=profile or data['gate']!=hardware_gate(data): raise ValueError('hardware evidence drift')
        records.append({'profile':profile,'evidence_fingerprint':fingerprint(data),'feasible':data['gate']['feasible']})
    result={'matrix_fingerprint':declaration['matrix_fingerprint'],'records':records}
    result['fingerprint']=fingerprint(result)
    target=directory/'feasibility.json'
    if target.exists():
        if json.loads(target.read_text())!=result: raise ValueError('frozen feasible list changed')
    else:
        with target.open('x',encoding='utf-8') as stream: json.dump(result,stream,indent=2)
    return result


def run_feasible_selection(hardware_directory,bundle_path,output_directory,*,completed_evidence=None):
    from tests.phase12_local.bge_probe import run_gated
    def run():
        import os
        import subprocess
        import sys
        root=Path(__file__).resolve().parents[2]
        frozen=freeze_hardware_feasibility(hardware_directory)
        target=Path(output_directory); target.mkdir(parents=True,exist_ok=True)
        rows=[]
        for record in frozen['records']:
            if not record['feasible']: continue
            profile=record['profile']; path=target/(profile['profile_id']+'.json')
            if path.exists():
                rows.append(read_completed_selection(path,completed_evidence or {},profile['profile_id']))
                continue
            hardware_path=Path(hardware_directory)/(profile['profile_id']+'.json')
            hardware=json.loads(hardware_path.read_text())
            if fingerprint(hardware)!=record['evidence_fingerprint']: raise ValueError('hardware evidence changed')
            child=subprocess.run([sys.executable,'-B','-m','tests.phase12_local.parameter_selection',
                '--hardware',str(hardware_path.resolve()),'--snapshot',str(Path(bundle_path).resolve()),
                '--output',str(path.resolve())],cwd=root,env=dict(os.environ,PHASE12_BGE_PROBE_ENABLED='1'),
                capture_output=True,text=True)
            if child.returncode:
                (target/'selection-stop.tmp').write_text(child.stderr,encoding='utf-8')
                raise RuntimeError(f'Selection experiment stopped: {profile["profile_id"]}')
            row=json.loads(path.read_text()); rows.append(row)
            print(json.dumps({'profile_id':profile['profile_id'],'gate':row['gate'],'metrics':row.get('variant')}),flush=True)
        decision=select_profile(rows)
        decision['matrix_fingerprint']=frozen['matrix_fingerprint']
        decision['feasibility_fingerprint']=frozen['fingerprint']
        decision['final_run_count']=0
        decision['batch_comparison']=batch_equivalence([r for r in rows if not r.get('selection_rejection')])
        (target/'decision.json').write_text(json.dumps(decision,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return decision
    return run_gated(run)


def render_reports(hardware_directory,selection_directory,output):
    """Derived human-readable report; raw experiment files remain the evidence."""
    hardware_directory=Path(hardware_directory); selection_directory=Path(selection_directory)
    hardware=[json.loads(p.read_text()) for p in sorted(hardware_directory.glob('*-B*.json'))]
    all_rows=[json.loads(p.read_text()) for p in sorted(selection_directory.glob('*-B*.json'))]
    quality=[r for r in all_rows if not r.get('selection_rejection')]
    rejected=[r for r in all_rows if r.get('selection_rejection')]
    decision=json.loads((selection_directory/'decision.json').read_text())
    lines=['## Stage A 全矩阵','',
           '|profile|feasible|warm n|warm p50/p95 ms|incremental p50/p95 ms|GPU MiB|OOM|reject reasons|',
           '|---|---|---:|---:|---:|---:|---|---|']
    def timing(g,key):
        s=g.get(key)
        return f"{s['p50_ms']:.2f}/{s['p95_ms']:.2f}" if s else 'not measured'
    for r in hardware:
        g=r['gate']
        lines.append(f"|{r['profile_id']}|{g['feasible']}|{len(r['warm_ms'])}|{timing(g,'warm')}|{timing(g,'incremental')}|{r['gpu_peak_mib']:.2f}|{r['failure_reason']=='oom'}|{', '.join(g['reasons'])}|")
    lines+=['','未达到20个样本或完整finite/stable Gate的profile不能判feasible；提前终止保留实际样本数。设备采样存在漏采瞬时峰值的限制。','',
        '## Stage B 全部feasible profile','',
        '|profile|nDCG@8|MRR@8|HR1|HR3|Recall8|coverage|Recall ceiling|Gate|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---|']
    metric_order=('ndcg_at_8','mrr_at_8','hit_rate_at_1','hit_rate_at_3','recall_at_8','candidate_coverage','recall_at_8_ceiling')
    baselines={}
    for r in quality:
        c=r['profile']['candidate_limit']
        if c in baselines and baselines[c]!=r['baseline']: raise ValueError('same-C baseline changed')
        baselines[c]=r['baseline']
        lines.append('|'+r['profile_id']+'|'+'|'.join(f"{r['variant'][k]:.6f}" for k in metric_order)+'|'+r['gate']['status']+'|')
    lines+=['','### Selection实际运行拒绝（不产生部分quality metrics）','']
    for r in rejected:
        lines.append('- `'+r['profile_id']+'`: '+json.dumps(r['selection_rejection'],ensure_ascii=False))
    lines+=['','### 同候选池RRF基线','',
        '|C|nDCG@8|MRR@8|HR1|HR3|Recall8|coverage|Recall ceiling|',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for c,r in sorted(baselines.items()): lines.append('|'+str(c)+'|'+'|'.join(f'{r[k]:.6f}' for k in metric_order)+'|')
    lines+=['','## 八类别与切片（baseline → variant，delta）']
    for r in quality:
        lines+=['',f"### {r['profile_id']}",'',
                '|category|count|nDCG@8|MRR@8|HR1|HR3|Recall8|',
                '|---|---:|---|---|---|---|---|']
        for cat,values in r['categories'].items():
            if cat=='robustness': continue
            b,v=values['baseline'],values['variant']
            lines.append('|'+cat+'|'+str(v['quality_query_count'])+'|'+'|'.join(f'{b[k]:.4f} → {v[k]:.4f} ({v[k]-b[k]:+.4f})' for k in metric_order[:5])+'|')
        tables=r['report']['by_capacity'][str(r['profile']['candidate_limit'])]
        lines+=['',f"Category review: `{json.dumps(r['gate']['category_review_required'],ensure_ascii=False)}`。",'',
            'Selection structured_table全为HTML：overall与HTML切片均为上表structured_table（5题）；Markdown count=0，五项metrics=null。long_paragraph为上表独立5题。',
            f"重复稳定性：40题各两次，最大score drift={max(x['max_score_drift'] for x in r['repeat_stability']):.6f}，ranking全稳定。",'']
    lines+=['## 成本选择与Owner边界','',f"状态：**{decision['status']}**。",'',
        f"推荐profile：`{decision.get('recommended_profile_id')}`。",'',
        f"被支配项：`{json.dumps(decision.get('dominated_profile_ids',[]))}`。",'',
        f"成本排序：`{json.dumps(decision.get('cost_order',[]))}`。",'',
        f"需类别Review的总体通过项：`{json.dumps(decision.get('category_review_profile_ids',[]))}`。",'',
        '没有修改质量/SLO门槛，没有启用reranker，没有写入正式production默认；Final run count=0。','',
        '## Batch / micro-batch排序比较','']
    for row in decision.get('batch_comparison',[]): lines.append('- `'+json.dumps(row,ensure_ascii=False)+'`')
    Path(output).write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return {'hardware_profiles':len(hardware),'feasible_profiles':sum(r['gate']['feasible'] for r in hardware),
            'quality_profiles':len(quality),'decision':decision['status']}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='M5 Selection only, Stage A feasible profiles required')
    parser.add_argument('--hardware',required=True)
    parser.add_argument('--snapshot',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    hardware=json.loads(Path(args.hardware).read_text())
    run_selection_profile(hardware['profile'],hardware,args.snapshot,args.output)
