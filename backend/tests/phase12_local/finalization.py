"""Owner-authorized M4 freeze and opt-in Development smoke; no import-time I/O.

The approved qrels define this frozen evaluation, not exhaustive corpus truth.
Live reads use PostgreSQL read-only transactions and OpenSearch query APIs only.
"""
from copy import deepcopy
import math
from time import perf_counter

from tests.phase12_local.corpus_audit import catalog, content_hash, eligibility, fingerprint
from tests.phase12_local.evaluation import baseline_ranking, evaluate, rank_scores, validate_manifest
from tests.phase12_local.bge_probe import run_gated


def substantive_identity(manifest):
    review_keys={'review_status','qrel_completeness_status','owner_review_reference',
                 'reviewer_role','review_date','review_reference'}
    return fingerprint({k:[{a:b for a,b in row.items() if a not in review_keys} for row in manifest[k]]
                        for k in ('queries','groups')})


def approve_and_freeze(manifest, corpus, date):
    validate_manifest(manifest,corpus)
    result=deepcopy(manifest)
    metadata={'review_status':'approved','reviewer_role':'project_owner','review_date':date,
              'review_reference':'Phase12 M4 owner approval'}
    result.update(metadata)
    result.update(status='frozen',annotation_protocol=(
        'Source-only drafts, accepted unchanged by project owner. Approved qrels are Phase 12 '
        'frozen evaluation gold, not exhaustive global truth of all potentially relevant corpus chunks. '
        'Unjudged chunks are not human-confirmed irrelevant.'))
    for row in (*result['queries'],*result['groups']): row.update(metadata)
    for q in result['queries']:
        q.update(qrel_completeness_status='approved',owner_review_reference=metadata['review_reference'])
    result.pop('frozen_manifest_fingerprint',None)
    result['frozen_manifest_fingerprint']=fingerprint(result)
    if substantive_identity(result)!=substantive_identity(manifest):
        raise ValueError('substantive Golden mutation forbidden')
    validate_manifest(result,corpus,require_review=True)
    return result


def read_only_session():
    from sqlalchemy import text
    from app.db.session import SessionLocal
    db=SessionLocal()
    try:
        db.connection(execution_options={'isolation_level':'REPEATABLE READ','postgresql_readonly':True})
        if db.execute(text("select current_setting('transaction_read_only')")).scalar_one()!='on':
            raise ValueError('read-only transaction required')
        return db
    except BaseException:
        db.close()
        raise


def validate_live_index(chunks, sources):
    indexed={}
    for source in sources:
        cid=source.get('chunk_id')
        if cid in indexed: raise ValueError('duplicate index chunk')
        vector=source.get('embedding',[])
        indexed[cid]={k:source.get(k) for k in ('document_id','embedding_model','embedding_dim','embedding_status')}
        indexed[cid].update(content_fingerprint=content_hash(source.get('content','')),
            lexical_present=bool(source.get('content')),
            vector_present=isinstance(vector,list) and len(vector)==1024
                and all(type(v) in (int,float) and math.isfinite(v) for v in vector))
    if set(indexed)!={c['chunk_id'] for c in chunks}: raise ValueError('index identity drift')
    for chunk in chunks:
        if eligibility({'document_id':chunk['document_id'],'deletion_status':'normal'},[chunk],indexed):
            raise ValueError('index/embedding missing or stale')


def verify_live_corpus(expected, settings=None):
    from datetime import datetime, timezone
    from uuid import UUID
    from sqlalchemy import text
    from app.core.config import get_settings
    from app.search_engine.client import get_search_engine_client
    settings=settings or get_settings()
    if (settings.embedding_model!='Qwen3-Embedding-0.6B' or settings.embedding_dim!=1024
            or not settings.embedding_local_files_only):
        raise ValueError('embedding config identity drift')
    def normalize(value):
        if isinstance(value,UUID): return str(value)
        if isinstance(value,datetime): return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
        return value
    with read_only_session() as db:
        docs=[{k:normalize(v) for k,v in r.items()} for r in db.execute(text(
            'SELECT id AS document_id,original_filename,file_type,file_hash,process_status,deletion_status,created_at,updated_at FROM documents ORDER BY id')).mappings()]
        chunks=[{k:normalize(v) for k,v in r.items()} for r in db.execute(text(
            'SELECT id AS chunk_id,document_id,parse_run_id,chunk_index,content,page_start,page_end,section_title,chunk_type,chunk_method,content_format,source_metadata,embedding_model,embedding_dim,embedding_status,embedding IS NOT NULL AS has_embedding,vector_dims(embedding) AS actual_embedding_dim,embedding_updated_at FROM document_chunks ORDER BY document_id,chunk_index')).mappings()]
    for c in chunks: c['content_fingerprint']=content_hash(c['content'])
    client=get_search_engine_client(settings)
    try:
        alias=expected['opensearch']['alias']
        resolved=client.indices.get_alias(name=alias)
        mapping=client.indices.get_mapping(index=alias)
        indices=client.indices.get_settings(index=alias)
        identity={'alias':alias,'resolved':resolved,'mapping_sha256':fingerprint(mapping),
            'indices':{name:{k:indices[name]['settings']['index'][k] for k in ('uuid','version','knn','creation_date')}
                       for name in sorted(resolved)}}
        sources=[]; after=None
        while True:
            body={'query':{'match_all':{}},'size':500,'sort':[{'chunk_id':'asc'}],
                  '_source':['chunk_id','document_id','content','embedding','embedding_model','embedding_dim','embedding_status']}
            if after is not None: body['search_after']=after
            hits=client.search(index=alias,body=body)['hits']['hits']
            if not hits: break
            sources.extend(h['_source'] for h in hits); after=hits[-1]['sort']
        validate_live_index(chunks,sources)
        actual=catalog(docs,chunks,identity,expected['code_commit'])
        if actual['corpus_fingerprint']!=expected['corpus_fingerprint']:
            raise ValueError('dataset stale: live corpus fingerprint changed')
        if any(d['deletion_status']!='normal' for d in docs): raise ValueError('deleted corpus')
        return {c['chunk_id']:c['content'] for c in chunks}, {
            'corpus_fingerprint':actual['corpus_fingerprint'],'document_count':len(docs),
            'chunk_count':len(chunks),'lexical_vector_count':len(sources),
            'read_only_transaction_verified':True,'integrity':'PASS',
            'checked_at_utc':datetime.now(timezone.utc).isoformat()}
    finally: client.close()


def development_smoke(manifest,corpus,snapshots,contents,service_factory):
    def run():
        queries={q['query_id']:q for q in manifest['queries'] if q['split']=='development'}
        if any(s['query_id'] not in queries or s['candidate_limit']!=8 for s in snapshots):
            raise ValueError('M4 smoke requires Development C8 only')
        validate_manifest(manifest,corpus,require_review=True)
        # Validate the complete candidate/query coverage before loading any model.
        dummy={f"{s['query_id']}:C8":[dict(chunk_id=i['chunk_id'],original_rank=i['original_rank'],raw_score=0.) for i in s['items']] for s in snapshots}
        evaluate(manifest,corpus,snapshots,dummy,split='development')
        from app.retrieval.reranker import RerankRequest,RerankCandidate
        scores={}; timings=[]
        service=service_factory()
        try:
            for snapshot in snapshots:
                baseline_ranking(snapshot,corpus)
                for item in snapshot['items']:
                    if content_hash(contents[item['chunk_id']])!=item['content_fingerprint']:
                        raise ValueError('dataset stale: scoring content')
                q=queries[snapshot['query_id']]
                request=RerankRequest(q['query_id'],q['query'],tuple(RerankCandidate(i['chunk_id'],i['original_rank'],contents[i['chunk_id']]) for i in snapshot['items']))
                start=perf_counter(); result=service.rerank(request)
                elapsed=(perf_counter()-start)*1000
                if result.failure_reason or result.request_id!=request.request_id:
                    raise ValueError(f'Development smoke reranker failed: {result.failure_reason}, request_id={request.request_id}, wall_ms={elapsed:.3f}')
                rows=[dict(chunk_id=s.chunk_id,original_rank=s.original_rank,raw_score=s.raw_score) for s in result.scores]
                rank_scores(snapshot,rows)
                scores[f"{q['query_id']}:C8"]=rows
                timings.append({'query_id':q['query_id'],'wall_ms':elapsed})
            return {'purpose':'Development wiring smoke, not parameter selection or quality gate',
                    'golden_fingerprint':manifest['frozen_manifest_fingerprint'],
                    'snapshot_fingerprint':fingerprint(snapshots),'variants':scores,'timings':timings,
                    'evaluation':evaluate(manifest,corpus,snapshots,scores,split='development')}
        finally: service.close()
    return run_gated(run)


def smoke_settings(base):
    from app.retrieval.reranker import MODEL_ID
    return base.model_copy(update=dict(reranker_enabled=True,reranker_provider='local_transformers',
        reranker_model=MODEL_ID,reranker_device='cuda',reranker_dtype='fp16',
        reranker_candidate_limit=8,reranker_batch_size=8,reranker_max_length=1024,reranker_timeout_seconds=5.0))


def run_development_smoke(output):
    def run():
        import hashlib
        import json
        import os
        from pathlib import Path
        from app.core.config import get_settings
        from app.retrieval.embeddings import get_embedding_provider
        from app.retrieval.local_cross_encoder import LocalCrossEncoderProvider, _load_local_model
        from app.services.reranking import RerankingService
        from app.services.hybrid_search import hybrid_search_chunks
        from app.search_engine.client import get_search_engine_client
        from tests.phase12_local.candidate_snapshots import probe_prefix
        os.environ['HF_HUB_OFFLINE']=os.environ['TRANSFORMERS_OFFLINE']='1'
        root=Path(__file__).resolve().parents[2]
        fixtures=root/'tests/fixtures/phase12'
        read=lambda p:json.loads(p.read_text(encoding='utf-8'))
        manifest=read(fixtures/'golden_manifest.json'); corpus=read(fixtures/'corpus_manifest.json')
        validate_manifest(manifest,corpus,require_review=True)
        contents,audit=verify_live_corpus(corpus)
        source=read(fixtures/'candidate_snapshots_development.json')
        if fingerprint(source['snapshots'])!=source['snapshot_fingerprint']:
            raise ValueError('snapshot fingerprint changed')
        snapshots=[s for s in source['snapshots'] if s['candidate_limit']==8]
        cfg=smoke_settings(get_settings())
        identity=read(root.parent/'docs/phase-12-m0-results/model-identity.json')
        for entry in identity['files']:
            with (Path(cfg.reranker_model_path)/entry['name']).open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=entry['sha256']:
                    raise ValueError('model identity changed')
        # Only the Development robustness query lacked a snapshot at draft time.
        embedding=get_embedding_provider(cfg)
        client=get_search_engine_client(cfg)
        try:
            def retrieve(query,limit):
                return hybrid_search_chunks(None,query=query,limit=limit,settings=cfg,client=client,
                    embedding_provider=embedding,deletion_filter_session_factory=read_only_session)
            robustness=[q for q in manifest['queries'] if q['split']=='development' and q['category']=='robustness']
            extra=probe_prefix(robustness,retrieve,corpus,'m4-development-robustness-freeze-20260916')
            snapshots.extend(s for s in extra['snapshots'] if s['candidate_limit']==8)
        finally: client.close()
        loads=[]
        def loader(config):
            loads.append(1)
            return _load_local_model(config)
        def factory():
            return RerankingService(cfg,provider_factory=lambda config:LocalCrossEncoderProvider(config,model_loader=loader))
        report=development_smoke(manifest,corpus,snapshots,contents,factory)
        _,after=verify_live_corpus(corpus)
        report.update(profile=dict(model_id=cfg.reranker_model,model_revision=identity['revision'],
            dtype='fp16',candidate_limit=8,batch_size=8,max_length=1024,timeout_seconds=5.0),
            model_load_count=len(loads),live_audit_before=audit,live_audit_after=after,
            final_run_count=0,selection_run_count=0,snapshots=snapshots)
        Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        return {'model_load_count':len(loads),'real_requests':len(snapshots),
                'metrics':report['evaluation']['by_capacity']['8'],
                'golden_fingerprint':manifest['frozen_manifest_fingerprint']}
    return run_gated(run)
