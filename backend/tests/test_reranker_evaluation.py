import copy
import importlib

import pytest


def module():
    return importlib.import_module('tests.phase12_local.evaluation')


def fixtures():
    from tests.phase12_local.corpus_audit import corpus_identity
    chunks=[{'chunk_id':n,'document_id':'doc-'+n,'content_fingerprint':n*64,
             'table_formats':['html'],'char_count':600} for n in ('a','b','c')]
    corpus={'schema_version':'phase12-golden-v1','corpus_fingerprint':'corpus',
            'opensearch':{'index':'v1'},'documents':[{'document_id':'doc-'+n,'deletion_status':'normal'} for n in ('a','b','c')],
            'chunks':chunks}
    queries=[{'query_id':'q-'+n,'query':q,'category':cat,'split':sp,'group_id':'g-'+n,
              'annotation_status':'draft','review_status':'OWNER_REVIEW_REQUIRED',
              'qrels':[{'chunk_id':n,'document_id':'doc-'+n,'grade':2,'content_fingerprint':n*64,'annotation_reason':'Direct evidence.'}]}
             for n,q,cat,sp in [('a','What is flow turbulence?','definition','development'),
                               ('b','How much core clearance?','numeric','selection'),
                               ('c','Which locking device is required?','multi_condition','final')]]
    groups=[{'group_id':'g-'+n,'split':sp,'document_ids':['doc-'+n],'source_ids':['source-'+n],
             'logical_topics':['topic-'+n]} for n,sp in [('a','development'),('b','selection'),('c','final')]]
    manifest={'schema_version':'phase12-golden-v1','status':'draft','corpus_fingerprint':'corpus',
              'opensearch':{'index':'v1'},'groups':groups,'queries':queries}
    snapshot={'query_id':'q-a','candidate_limit':8,'retrieval_run_id':'run','corpus_fingerprint':'corpus',
              'items':[{'chunk_id':n,'document_id':'doc-'+n,'original_rank':i,'hybrid_score':.1,
                        'keyword_rank':i,'vector_rank':None,'content_fingerprint':n*64}
                       for i,n in enumerate(('b','a'),1)]}
    corpus['code_commit']='synthetic-test-head'
    digest=corpus_identity(corpus['documents'],corpus['chunks'],corpus['opensearch'],corpus['code_commit'])
    corpus['corpus_fingerprint']=manifest['corpus_fingerprint']=snapshot['corpus_fingerprint']=digest
    return manifest,corpus,snapshot


def validate(m,c,**kwargs):
    return module().validate_manifest(m,c,require_coverage=False,**kwargs)


def test_draft_valid_but_never_approved():
    m,c,_=fixtures(); validate(m,c)
    assert all(q['review_status']=='OWNER_REVIEW_REQUIRED' for q in m['queries'])


@pytest.mark.parametrize('mutation,reason', [
    (lambda m,c:m['queries'].append(copy.deepcopy(m['queries'][0])), 'duplicate query'),
    (lambda m,c:m['queries'][0]['qrels'].append(copy.deepcopy(m['queries'][0]['qrels'][0])), 'duplicate qrel'),
    (lambda m,c:c['chunks'].append(copy.deepcopy(c['chunks'][0])), 'duplicate chunk'),
    (lambda m,c:c['documents'].pop(0), 'missing document'),
    (lambda m,c:m['queries'][0]['qrels'][0].pop('content_fingerprint'), 'fingerprint'),
    (lambda m,c:m['queries'][0]['qrels'][0].update(chunk_id='unknown'), 'unknown qrel'),
    (lambda m,c:m['queries'][0]['qrels'][0].update(content_fingerprint='changed'), 'stale'),
    (lambda m,c:m.update(corpus_fingerprint='changed'), 'stale'),
    (lambda m,c:c.update(opensearch={'index':'v2'}), 'stale'),
    (lambda m,c:m['queries'][2].update(group_id='g-b'), 'group'),
    (lambda m,c:m['groups'][2]['source_ids'].append('source-b'), 'leakage'),
    (lambda m,c:m['groups'][2]['document_ids'].append('doc-b'), 'leakage'),
    (lambda m,c:m['groups'][2]['logical_topics'].append('topic-b'), 'leakage'),
    (lambda m,c:m['queries'][2].update(query=m['queries'][0]['query']), 'near-duplicate'),
])
def test_integrity_rejections(mutation,reason):
    m,c,_=fixtures(); mutation(m,c)
    with pytest.raises(ValueError,match=reason): validate(m,c)


@pytest.mark.parametrize('status',['deleting','delete_failed','missing'])
def test_deleted_gold(status):
    m,c,_=fixtures(); c['documents'][0]['deletion_status']=status
    with pytest.raises(ValueError,match='stale'): validate(m,c)


@pytest.mark.parametrize('split',['selection','final','development'])
def test_unreviewed_execution_rejected(split):
    m,c,_=fixtures()
    with pytest.raises(ValueError,match='OWNER_REVIEW_REQUIRED'):
        validate(m,c,require_review=True)


def test_structured_table_requires_format_and_dependency():
    m,c,_=fixtures(); q=m['queries'][0]; q['category']='structured_table'
    with pytest.raises(ValueError,match='table_format'): validate(m,c)
    q['table_format']='html'
    with pytest.raises(ValueError,match='table evidence'): validate(m,c)
    q['table_evidence']={'chunk_id':'a','dependency':'row_column','locator':'row WCB / energy column'}
    validate(m,c)
    q['table_format']='markdown'
    with pytest.raises(ValueError,match='table format'): validate(m,c)


def test_baseline_preserves_snapshot_order_even_equal_float_scores():
    _,c,s=fixtures()
    assert module().baseline_ranking(s,c)==['b','a']


def test_score_sort_and_identity_mapping():
    _,_,s=fixtures()
    scores=[{'chunk_id':'a','original_rank':2,'raw_score':3.0},
            {'chunk_id':'b','original_rank':1,'raw_score':2.0}]
    assert module().rank_scores(s,scores)==['a','b']
    scores[1]['raw_score']=3.0
    assert module().rank_scores(s,scores)==['b','a']


@pytest.mark.parametrize('mutation',[
    lambda s:s.pop(), lambda s:s.append(dict(s[0])),
    lambda s:s[0].update(chunk_id='unknown'), lambda s:s[0].update(original_rank=99),
    lambda s:s[0].update(raw_score=float('nan')),lambda s:s[0].update(raw_score=float('inf')),
])
def test_invalid_scores_all_or_nothing(mutation):
    _,_,snapshot=fixtures()
    scores=[{'chunk_id':i['chunk_id'],'original_rank':i['original_rank'],'raw_score':1.} for i in snapshot['items']]
    mutation(scores)
    with pytest.raises(ValueError): module().rank_scores(snapshot,scores)


def test_duplicate_snapshot_invalid():
    _,c,s=fixtures(); s['items'].append(dict(s['items'][0]))
    with pytest.raises(ValueError,match='duplicate'): module().baseline_ranking(s,c)


def test_same_pool_extra_missing_or_duplicate_variant_invalid():
    for ranking in (['a'],['a','unknown'],['a','a']):
        with pytest.raises(ValueError): module().same_pool(['a','b'],ranking)


def test_prefix_consistency_exact_order():
    small=list(range(8)); mid=list(range(16)); large=list(range(32))
    assert module().prefix_consistent({8:small,16:mid,32:large})
    assert not module().prefix_consistent({8:small[::-1],16:mid,32:large})


def test_final_gate_precedes_any_scoring():
    with pytest.raises(ValueError,match='final held-out requires explicit owner authorization'):
        module().evaluate({}, {}, [], {}, split='final')


def test_selection_only_m5_and_development_requires_owner_freeze():
    m,c,s=fixtures()
    with pytest.raises(ValueError,match='Selection is reserved for M5'):
        module().evaluate(m,c,[s],{},split='selection')
    with pytest.raises(ValueError,match='OWNER_REVIEW_REQUIRED'):
        module().evaluate(m,c,[s],{},split='development')


def test_slices_aggregate_robustness_and_coverage():
    metrics=importlib.import_module('tests.phase12_local.metrics')
    rows=[]
    for cat,fmt,grade in [('structured_table','markdown',2),('structured_table','html',1),('long_paragraph',None,2),('robustness',None,0)]:
        rows.append({'category':cat,'table_format':fmt,'metrics':metrics.query_metrics(['a'],{'a':grade},['a'])})
    r=module().aggregate(rows)
    assert r['overall']['quality_query_count']==3
    assert r['overall']['robustness_query_count']==1
    assert r['categories']['structured_table']['quality_query_count']==2
    assert r['table_formats']['html']['recall_at_8']==1
    assert r['table_formats']['markdown']['quality_query_count']==1
    assert r['categories']['long_paragraph']['candidate_coverage']==1


def test_embedding_identity_cannot_drift_behind_unchanged_digest():
    from pathlib import Path
    import json
    p = Path(__file__).parent / 'fixtures' / 'phase12'
    m = json.loads((p/'golden_manifest.json').read_text(encoding='utf-8'))
    c = json.loads((p/'corpus_manifest.json').read_text(encoding='utf-8'))
    c['chunks'][0]['embedding_model'] = 'different-model'
    with pytest.raises(ValueError, match='stale'):
        module().validate_manifest(m, c)


def test_canonical_draft_coverage_pending_review_and_no_bodies():
    from collections import Counter
    from pathlib import Path
    import json
    p = Path(__file__).parent / 'fixtures' / 'phase12'
    m = json.loads((p/'golden_manifest.json').read_text(encoding='utf-8'))
    c = json.loads((p/'corpus_manifest.json').read_text(encoding='utf-8'))
    module().validate_manifest(m, c)
    counts = Counter((q['split'], q['category']) for q in m['queries'])
    assert all(counts[s,cat]>=5 for s in module().SPLITS for cat in module().CATEGORIES)
    assert len([q for q in m['queries'] if q['category']!='robustness']) >= 120
    if m['status']!='frozen':
        assert 'frozen_manifest_fingerprint' not in m
    else:
        module().validate_manifest(m,c,require_review=True)
    assert all('content' not in chunk and 'embedding' not in chunk for chunk in c['chunks'])


def synthetic_reviewed_bundle():
    """Only synthetic IDs/evidence; never approve the actual Golden fixture."""
    from tests.phase12_local.corpus_audit import content_hash, corpus_identity, fingerprint
    m,c,s=fixtures()
    # Exercise full coverage validation with synthetic, clearly different split text.
    m['queries']=[]
    for n,group,prompt in zip('abc',m['groups'],('water flow '*4,'geometric dimension '*4,'machine safety '*4)):
        c['chunks'].append(dict(c['chunks'][ord(n)-97],chunk_id=n+'-text',table_formats=[]))
        c['chunks'][ord(n)-97]['table_formats']=['markdown' if n=='a' else 'html']
        for cat in module().CATEGORIES:
            for j in range(5):
                cid=n if cat=='structured_table' else n+'-text'
                q={'query_id':f'{n}-{cat}-{j}','query':prompt+cat+str(j),
                   'category':cat,'split':group['split'],'group_id':group['group_id'],
                   'annotation_status':'synthetic_test','review_status':'approved',
                   'qrel_completeness_status':'approved','owner_review_reference':'unit-only',
                   'qrels':[{'chunk_id':cid,'document_id':'doc-'+n,'grade':2,
                             'content_fingerprint':n*64,'annotation_reason':'Synthetic evidence.'}]}
                if cat=='structured_table':
                    q.update(table_format='markdown' if n=='a' else 'html',
                             table_evidence={'chunk_id':cid,'dependency':'row_column','locator':'synthetic cell'})
                m['queries'].append(q)
    for g in m['groups']: g['review_status']='approved'
    digest=corpus_identity(c['documents'],c['chunks'],c['opensearch'],c['code_commit'])
    c['corpus_fingerprint']=m['corpus_fingerprint']=s['corpus_fingerprint']=digest
    m['status']='frozen'
    m['frozen_manifest_fingerprint']=fingerprint(m)
    snapshots=[]; variants={}
    for q in m['queries']:
        if q['split']!='development': continue
        for capacity in (8,16):
            row=copy.deepcopy(s); row.update(query_id=q['query_id'],query_fingerprint=content_hash(q['query']),candidate_limit=capacity)
            snapshots.append(row)
            variants[f"{q['query_id']}:C{capacity}"]=[{'chunk_id':i['chunk_id'],'original_rank':i['original_rank'],'raw_score':float(i['chunk_id']=='a')} for i in row['items']]
    return m,c,snapshots,variants


def test_complete_report_separates_capacities_and_preserves_denominator():
    m,c,s,v=synthetic_reviewed_bundle()
    result=module().evaluate(m,c,s,v,split='development')
    assert set(result['by_capacity'])=={'8','16'}
    assert len(result['queries'])==80
    for capacity in result['by_capacity'].values():
        assert capacity['baseline']['overall']['quality_query_count']==40
        assert capacity['variant']['table_formats']['markdown']['hit_rate_at_1']==1
        # Most synthetic gold is outside the pool; never drop it from denominators.
        assert capacity['variant']['overall']['recall_at_8']==pytest.approx(5/40)


def test_snapshot_from_old_query_rejected():
    m,c,s,v=synthetic_reviewed_bundle(); s[0]['query_fingerprint']='old-query'
    with pytest.raises(ValueError,match='query identity'):
        module().evaluate(m,c,s,v,split='development')


def test_extra_variant_rejected():
    m,c,s,v=synthetic_reviewed_bundle(); v['unknown-query:C8']=[]
    with pytest.raises(ValueError,match='variant keys'):
        module().evaluate(m,c,s,v,split='development')


def test_missing_one_capacity_for_one_query_rejected():
    m,c,s,v=synthetic_reviewed_bundle()
    removed=s.pop()
    v.pop(f"{removed['query_id']}:C{removed['candidate_limit']}")
    with pytest.raises(ValueError,match='capacity coverage'):
        module().evaluate(m,c,s,v,split='development')


def test_qrel_completeness_requires_human_review():
    m,c,s,v=synthetic_reviewed_bundle()
    m['queries'][0]['qrel_completeness_status']='OWNER_REVIEW_REQUIRED'
    with pytest.raises(ValueError,match='OWNER_REVIEW_REQUIRED'):
        module().evaluate(m,c,s,v,split='development')


def test_model_free_core_import_in_fresh_process():
    import subprocess
    import sys
    result=subprocess.run([sys.executable,'-B','-c',
        "import sys; from tests.phase12_local import evaluation, metrics, candidate_snapshots; "
        "assert not any(n in sys.modules for n in ['torch','transformers','sentence_transformers','app.retrieval.local_cross_encoder'])"],
        capture_output=True,text=True)
    assert result.returncode==0, result.stderr


@pytest.mark.parametrize('kind',['source_file','positive_evidence'])
def test_duplicate_source_content_across_splits(kind):
    from tests.phase12_local.corpus_audit import corpus_identity
    m,c,_=fixtures()
    if kind=='source_file':
        c['documents'][1]['file_hash']=c['documents'][2]['file_hash']='same-original-file'
    else:
        c['chunks'][2]['content_fingerprint']=c['chunks'][1]['content_fingerprint']
        m['queries'][2]['qrels'][0]['content_fingerprint']=c['chunks'][1]['content_fingerprint']
    c['corpus_fingerprint']=m['corpus_fingerprint']=corpus_identity(c['documents'],c['chunks'],c['opensearch'],c['code_commit'])
    with pytest.raises(ValueError,match='leakage'):
        validate(m,c)


def test_cli_final_gate_precedes_file_read(monkeypatch):
    import sys
    monkeypatch.setattr(sys,'argv',['evaluation','--split','final','--manifest','missing','--corpus','missing',
                                   '--snapshots','missing','--variants','missing','--output','never-written'])
    with pytest.raises(SystemExit) as exc:
        module().main()
    assert exc.value.code==2
