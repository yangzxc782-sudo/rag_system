"""Explicit M5 synthetic hardware experiments; no import-time ML or I/O."""
import math
from tests.phase12_local.bge_probe import run_gated,score_stability,summarize_latencies


def matrix():
    profiles=[]
    for dtype in ('fp16','bf16'):
        for length in (1024,2048,4096):
            for capacity in (8,16,32):
                batches=[b for b in (32,16,8) if b<=capacity]
                if length==4096: batches += [4,2,1]
                for batch in batches:
                    profiles.append(dict(profile_id=f'{dtype}-L{length}-C{capacity}-B{batch}',dtype=dtype,
                        max_length=length,candidate_limit=capacity,batch_size=batch,timeout_seconds=5.0))
    return profiles


def hardware_gate(row):
    reasons=[]; summary={}
    if row.get('failure_reason'): reasons.append(row['failure_reason'])
    if not isinstance(row.get('gpu_peak_mib'),(int,float)) or not math.isfinite(row['gpu_peak_mib']) or row['gpu_peak_mib']>6500:
        reasons.append('gpu_peak_rejected')
    for name,ceiling in (('warm',2000),('incremental',2200)):
        try:
            stats=summarize_latencies(row[name+'_ms'])
            summary[name]=stats; summary[name+'_p95_ms']=stats['p95_ms']
            if stats['p95_ms']>ceiling: reasons.append(name+'_latency_rejected')
        except (KeyError,ValueError,RuntimeError): reasons.append(name+'_samples_invalid')
    try:
        stability=score_stability(row['scores'],row['candidate_ids'])
        summary['stability']=stability
        if len(row['scores'])<20: reasons.append('score_samples_insufficient')
        if not stability['ranking_stable']: reasons.append('ranking_unstable')
    except (KeyError,ValueError,RuntimeError): reasons.append('score_invalid')
    return dict(summary,feasible=not reasons,reasons=reasons,gpu_peak_mib=row.get('gpu_peak_mib'))


def run_profile(profile,output):
    return run_gated(lambda:_real_profile(profile,output))


def warm_gate_passed(samples):
    try:
        return summarize_latencies(samples)['p95_ms']<=2000
    except RuntimeError:
        return False


def _real_profile(profile,output):
    import gc
    import hashlib
    import importlib.metadata
    import json
    import os
    from pathlib import Path
    from time import perf_counter
    from unittest.mock import patch
    from uuid import NAMESPACE_URL,uuid5
    if profile not in matrix(): raise ValueError('undeclared profile')
    if Path(output).exists(): raise ValueError('hardware result overwrite forbidden')
    os.environ['HF_HUB_OFFLINE']=os.environ['TRANSFORMERS_OFFLINE']='1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS']='1'
    import torch
    from app.core.config import get_settings
    from app.retrieval.embeddings import get_embedding_provider
    from app.retrieval.local_cross_encoder import LocalCrossEncoderProvider
    from app.retrieval.reranker import RerankerConfig,RerankCandidate,RerankRequest
    from app.services.reranking import RerankingService
    from app.services.hybrid_search import HybridSearchItem,HybridSearchResult
    from app.services.rag import optional_rerank_chunks
    from tests.phase12_local.finalization import smoke_settings
    from tests.phase12_local.bge_smoke import DeviceSampler
    from tests.phase12_local.bge_probe import gpu_snapshot
    root=Path(__file__).resolve().parents[2]
    cfg=smoke_settings(get_settings()).model_copy(update={
        'reranker_dtype':profile['dtype'],'reranker_max_length':profile['max_length'],
        'reranker_candidate_limit':profile['candidate_limit'],'reranker_batch_size':profile['batch_size']})
    identity=json.loads((root.parent/'docs/phase-12-m0-results/model-identity.json').read_text())
    for entry in identity['files']:
        with (Path(cfg.reranker_model_path)/entry['name']).open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=entry['sha256']: raise ValueError('model drift')
    if not torch.cuda.is_available(): raise RuntimeError('CUDA unavailable')
    if profile['dtype']=='bf16' and not torch.cuda.is_bf16_supported(): raise RuntimeError('BF16 unsupported')
    row=dict(profile_id=profile['profile_id'],profile=profile,model_id=identity['model_id'],
        model_revision=identity['revision'],runtime={k:importlib.metadata.version(k) for k in ('torch','transformers','sentence-transformers')},
        cuda=torch.version.cuda,device=torch.cuda.get_device_name(0),warm_ms=[],incremental_ms=[],
        scores=[],candidate_ids=[],failure_reason=None,gpu_peak_mib=0.,
        final_run_count=0,selection_labels_used=False)
    sampler=DeviceSampler(torch); sampler.thread.start()
    provider=None; service=None; embedding=None
    def memory():
        now=gpu_snapshot(torch)
        row['gpu']=now
        row['gpu_peak_mib']=max(row['gpu_peak_mib'],now['device_used_bytes']/2**20,
            max((v['device_used_bytes']/2**20 for _,v in sampler.samples),default=0.))
        return row['gpu_peak_mib']<=6500
    def checked(result):
        if result.failure_reason:
            row['failure_reason']=result.failure_reason
            if result.failure_reason not in ('oom','timeout'): raise RuntimeError('unexpected production inference failure')
            return False
        if not all(math.isfinite(s.raw_score) for s in result.scores):
            row['failure_reason']='score_nonfinite'; return False
        return True
    try:
        torch.cuda.reset_peak_memory_stats()
        embedding=get_embedding_provider(cfg)
        embedding.encode_query('合成硬件测试：铸造材料检验条件。')
        held_model_id=id(embedding._model)
        torch.cuda.synchronize(); row['embedding_loaded_gpu']=gpu_snapshot(torch)
        provider=LocalCrossEncoderProvider(RerankerConfig.from_settings(cfg))
        start=perf_counter(); provider._ensure_loaded(); torch.cuda.synchronize()
        loaded=provider._loaded
        row['cold_model_load_ms']=(perf_counter()-start)*1000
        query='合成材料在给定温度下如何检验强度和冲击性能？'
        candidates,token_rows=synthetic_workload(loaded.tokenizer,query,profile)
        row['token_workload']=token_rows
        request=RerankRequest('m5-hardware',query,tuple(RerankCandidate(c['id'],i,c['content']) for i,c in enumerate(candidates,1)))
        row['candidate_ids']=[c.chunk_id for c in request.candidates]
        service=RerankingService(cfg,provider_factory=lambda _:provider)
        torch.cuda.synchronize(); start=perf_counter(); first=service.rerank(request)
        row['first_forward_wall_ms']=(perf_counter()-start)*1000
        if checked(first) and memory():
            for _ in range(20):
                torch.cuda.synchronize(); start=perf_counter(); result=service.rerank(request)
                elapsed=(perf_counter()-start)*1000
                if not checked(result): break
                torch.cuda.synchronize()
                row['warm_ms'].append(elapsed)
                score_map={s.chunk_id:s.raw_score for s in result.scores}
                row['scores'].append([score_map[c] for c in row['candidate_ids']])
                if not memory(): break
            if warm_gate_passed(row['warm_ms']):
                items=[HybridSearchItem(chunk_id=c.chunk_id,document_id=str(uuid5(NAMESPACE_URL,'m5-synthetic-document')),
                    original_filename='synthetic-workload',chunk_index=i,content=c.content,source_metadata=None,
                    retrieval_source='hybrid',keyword_score=1.,vector_score=1.,keyword_rank=i,vector_rank=i,
                    hybrid_score=1/(60+i),matched_keywords=[],embedding_model='Qwen3-Embedding-0.6B',embedding_dim=1024)
                    for i,c in enumerate(request.candidates,1)]
                snapshot=HybridSearchResult(query=query,limit=profile['candidate_limit'],total=len(items),items=items)
                off=cfg.model_copy(update={'reranker_enabled':False})
                with patch('app.services.reranking.get_reranking_service',return_value=service):
                    for _ in range(20):
                        start=perf_counter(); baseline=optional_rerank_chunks(query,snapshot,off,limit=8)
                        baseline_ms=(perf_counter()-start)*1000
                        torch.cuda.synchronize(); start=perf_counter()
                        variant=optional_rerank_chunks(query,snapshot,cfg,limit=8)
                        variant_ms=(perf_counter()-start)*1000
                        if not variant.applied:
                            row['failure_reason']=variant.fallback_reason; break
                        torch.cuda.synchronize()
                        row['incremental_ms'].append(variant_ms-baseline_ms)
                        if not memory(): break
        service.wait_idle(60)
        torch.cuda.synchronize()
        if id(embedding._model)!=held_model_id: raise RuntimeError('embedding residency changed')
        row['embedding_retained']=True
        if provider._unavailable:
            raise RuntimeError('PHASE12_M5_CUDA_UNHEALTHY')
    except torch.cuda.OutOfMemoryError:
        row['failure_reason']='oom'
    finally:
        if service: service.close()
        elif provider: provider.close()
        memory(); sampler.close()
        row['device_sample_count']=len(sampler.samples)
        row['device_max_sampling_gap_ms']=max(((b[0]-a[0])*1000 for a,b in zip(sampler.samples,sampler.samples[1:])),default=0.)
        row['device_sampling_limitation']='Sampling cannot guarantee capture of peaks between observations.'
        if sampler.failed or not sampler.samples:
            raise RuntimeError('PHASE12_M5_CUDA_UNHEALTHY: device sampling unavailable')
    row['gate']=hardware_gate(row)
    Path(output).write_text(json.dumps(row,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return {'profile_id':row['profile_id'],**row['gate']}


def synthetic_workload(tokenizer,query,profile):
    """All bodies are synthetic; target near pair budget using the real tokenizer."""
    from uuid import NAMESPACE_URL,uuid5
    seeds=(
        '合成铸件检验要求：试样应经过相同热处理，记录温度、强度和试验条件。',
        '合成长段落：先确认原材料和炉批，再核对试样位置、处理时间、冷却条件、机械性能与复验记录。',
        '<table><tr><th>合成牌号</th><th>温度 ℃</th><th>强度 MPa</th></tr><tr><td>TEST-A</td><td>20</td><td>420</td></tr></table>',
    )
    overhead=tokenizer.num_special_tokens_to_add(pair=True)
    budget=profile['max_length']-len(tokenizer.encode(query,add_special_tokens=False))-overhead
    bodies=[]; audit=[]
    for i in range(profile['candidate_limit']):
        seed=seeds[i%3]+f' 合成序号{i}。'
        seed_length=len(tokenizer.encode(seed,add_special_tokens=False))
        repetitions=budget//seed_length+2
        for _ in range(8):
            ids=tokenizer.encode(seed*repetitions,add_special_tokens=False,truncation=False)[:budget]
            body=tokenizer.decode(ids,skip_special_tokens=True)
            pair=tokenizer(query,body,truncation='only_second',max_length=profile['max_length'])
            actual=len(pair['input_ids'])
            if .98*profile['max_length']<=actual<=profile['max_length']: break
            repetitions*=2
        else:
            raise ValueError('synthetic workload must exercise near pair budget')
        cid=str(uuid5(NAMESPACE_URL,f'm5-hardware-{i}'))
        bodies.append({'id':cid,'content':body})
        audit.append({'candidate_id':cid,'kind':('normal_text','long_text','structured_table')[i%3],
                      'pair_tokens':actual,'pair_special_tokens':overhead})
    return bodies,audit


def run_matrix(output_directory):
    """Separate sequential experiment processes, never a production process design."""
    def run():
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        from tests.phase12_local.corpus_audit import fingerprint
        root=Path(__file__).resolve().parents[2]
        target=Path(output_directory); target.mkdir(parents=True,exist_ok=True)
        matrix_path=target/'matrix.json'
        declared={'profiles':matrix(),'warm_samples':20,'incremental_samples':20,
            'workload':'synthetic normal/long/HTML near pair budget; no Selection/Final labels',
            'protocol':'all profiles explicit; independent processes; no adaptive profile after quality',
            'timeout_seconds':5.,'SLO':{'warm_p95_ms':2000,'incremental_p95_ms':2200,'gpu_peak_mib':6500}}
        declared['matrix_fingerprint']=fingerprint(declared)
        if matrix_path.exists():
            if json.loads(matrix_path.read_text())!=declared: raise ValueError('frozen matrix drift')
        else:
            with matrix_path.open('x',encoding='utf-8') as stream: json.dump(declared,stream,indent=2)
        for profile in declared['profiles']:
            path=target/(profile['profile_id']+'.json')
            if path.exists():
                data=json.loads(path.read_text())
                if data['profile']!=profile: raise ValueError('profile identity drift')
                continue
            child=subprocess.run([sys.executable,'-B','-m','tests.phase12_local.m5_hardware_screen',
                '--profile',profile['profile_id'],'--output',str(path.resolve())],cwd=root,
                env=dict(os.environ,PHASE12_BGE_PROBE_ENABLED='1'),capture_output=True,text=True)
            if child.returncode:
                # Never echo third-party traceback/content/paths into versioned reports.
                print(json.dumps({'profile_id':profile['profile_id'],'status':'stopped','exit_code':child.returncode}),flush=True)
                (target/'hardware-stop.tmp').write_text(child.stderr,encoding='utf-8')
                raise RuntimeError('M5 hardware experiment failed; inspect local diagnostic before continuing')
            data=json.loads(path.read_text()); gate=data['gate']
            print(json.dumps({'profile_id':profile['profile_id'],'feasible':gate['feasible'],
                'reasons':gate['reasons'],'warm_p95_ms':gate.get('warm_p95_ms'),
                'incremental_p95_ms':gate.get('incremental_p95_ms'),'gpu_peak_mib':gate['gpu_peak_mib']}),flush=True)
        return declared['matrix_fingerprint']
    return run_gated(run)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True,choices=[p['profile_id'] for p in matrix()])
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    run_profile(next(p for p in matrix() if p['profile_id']==args.profile),args.output)
