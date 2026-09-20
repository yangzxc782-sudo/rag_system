"""Test-only evaluation and integrity contracts. Core is model/storage independent.

Draft validation is allowed before owner review. Evaluation requires a reviewed,
frozen manifest and a freshly verified corpus. CLI cannot authorize Final.
"""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import math
import re
import unicodedata

from tests.phase12_local.corpus_audit import SCHEMA_VERSION, content_hash, corpus_identity, fingerprint
from tests.phase12_local.metrics import QUALITY_KEYS, query_metrics

CATEGORIES = ('definition','paraphrase','exact_identifier','numeric','multi_condition',
              'long_paragraph','structured_table','hard_negative')
SPLITS = ('development','selection','final')


def unique(rows, key, label):
    values = [row[key] for row in rows]
    if len(values) != len(set(values)):
        raise ValueError(f'duplicate {label}')


def _normalized_query(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    return re.sub(r'\d+', '#', ''.join(c for c in text if c.isalnum()))


def validate_manifest(manifest, corpus, *, require_review=False, require_coverage=True):
    queries = manifest['queries']; groups = manifest['groups']
    unique(queries, 'query_id', 'query ID'); unique(groups, 'group_id', 'group ID')
    unique(corpus['chunks'], 'chunk_id', 'chunk ID')
    unique(corpus['documents'], 'document_id', 'document ID')
    if (manifest['schema_version'] != SCHEMA_VERSION or corpus['schema_version'] != SCHEMA_VERSION
            or manifest['corpus_fingerprint'] != corpus['corpus_fingerprint']
            or manifest['opensearch'] != corpus['opensearch']):
        raise ValueError('dataset stale: corpus or OpenSearch identity changed')
    docs = {d['document_id']:d for d in corpus['documents']}
    chunks = {c['chunk_id']:c for c in corpus['chunks']}
    by_group = {g['group_id']:g for g in groups}
    # Source/document/topic keys are human-curated from originals, not filenames.
    for i, group in enumerate(groups):
        if group['split'] not in SPLITS or any(not group.get(k) for k in ('document_ids','source_ids','logical_topics')):
            raise ValueError('invalid group')
        if require_review and group.get('review_status') != 'approved':
            raise ValueError('OWNER_REVIEW_REQUIRED: source/topic groups')
        for other in groups[i+1:]:
            if group['split'] != other['split']:
                for key in ('document_ids','source_ids','logical_topics'):
                    if set(group[key]) & set(other[key]):
                        raise ValueError(f'split leakage: {key}')
    for q in queries:
        if q.get('split') not in SPLITS or q.get('category') not in (*CATEGORIES, 'robustness'):
            raise ValueError('invalid split/category')
        if not isinstance(q.get('query'),str) or not q['query'].strip():
            raise ValueError('query required')
        group = by_group.get(q['group_id'])
        if group is None or group['split'] != q['split']:
            raise ValueError('query group mismatch')
        if not q.get('annotation_status') or not q.get('review_status'):
            raise ValueError('annotation/review status required')
        if require_review and (q['review_status'] != 'approved' or not q.get('owner_review_reference')
                               or q.get('qrel_completeness_status') != 'approved'):
            raise ValueError('OWNER_REVIEW_REQUIRED')
        unique(q['qrels'], 'chunk_id', 'qrel')
        for rel in q['qrels']:
            if not rel.get('content_fingerprint'):
                raise ValueError('missing fingerprint')
            chunk = chunks.get(rel['chunk_id'])
            if chunk is None:
                raise ValueError('unknown qrel')
            document = docs.get(rel['document_id'])
            if document is None:
                raise ValueError('missing document')
            if (document['deletion_status'] != 'normal'
                    or chunk['document_id'] != rel['document_id']
                    or chunk['content_fingerprint'] != rel['content_fingerprint']):
                raise ValueError('dataset stale: gold content/deletion identity')
            if rel['document_id'] not in group['document_ids']:
                raise ValueError('qrel source outside query group')
            if type(rel.get('grade')) is not int or rel['grade'] not in (0,1,2) or not rel.get('annotation_reason'):
                raise ValueError('invalid qrel grade/reason')
        answerable = any(rel['grade']>0 for rel in q['qrels'])
        if answerable == (q['category']=='robustness'):
            raise ValueError('answerable/robustness separation required')
        if q['category']=='long_paragraph':
            if not any(r['grade']>0 and chunks[r['chunk_id']]['char_count']>=500
                       and not chunks[r['chunk_id']]['table_formats'] for r in q['qrels']):
                raise ValueError('long paragraph evidence required')
        if q['category']=='structured_table':
            if q.get('table_format') not in ('markdown','html'):
                raise ValueError('table_format required')
            evidence = q.get('table_evidence', {})
            if (evidence.get('dependency') not in ('header','row_column','cell_value','unit_range','condition','multi_row')
                    or not evidence.get('locator')
                    or evidence.get('chunk_id') not in {r['chunk_id'] for r in q['qrels'] if r['grade']>0}):
                raise ValueError('table evidence dependency required; tag presence is insufficient')
            if q['table_format'] not in chunks[evidence['chunk_id']]['table_formats']:
                raise ValueError('table format disagrees with actual source')
    if (not corpus.get('code_commit') or corpus_identity(corpus['documents'], corpus['chunks'],
            corpus['opensearch'], corpus['code_commit']) != corpus['corpus_fingerprint']):
        raise ValueError('dataset stale: corpus identity digest mismatch')
    source_hash_splits = {}
    for group in groups:
        for doc_id in group['document_ids']:
            if doc_id not in docs:
                raise ValueError('missing document in group')
            digest = docs[doc_id].get('file_hash')
            if digest:
                source_hash_splits.setdefault(digest, set()).add(group['split'])
    evidence_hash_splits = {}
    for q in queries:
        for rel in q['qrels']:
            if rel['grade'] > 0:
                evidence_hash_splits.setdefault(rel['content_fingerprint'], set()).add(q['split'])
    if any(len(splits)>1 for splits in (*source_hash_splits.values(), *evidence_hash_splits.values())):
        raise ValueError('source/content leakage across splits')
    for i, q in enumerate(queries):
        for other in queries[i+1:]:
            if q['split'] != other['split']:
                same_near_group = q.get('near_duplicate_group') and q.get('near_duplicate_group') == other.get('near_duplicate_group')
                similar = SequenceMatcher(None,_normalized_query(q['query']),_normalized_query(other['query']),autojunk=False).ratio()>=.85
                if same_near_group or similar:
                    raise ValueError('near-duplicate query crosses splits')
    if require_coverage:
        counts = Counter((q['split'],q['category']) for q in queries)
        if any(counts[split,cat]<5 for split in SPLITS for cat in CATEGORIES):
            raise ValueError('insufficient split/category coverage')
        if {q.get('table_format') for q in queries if q['category']=='structured_table'} != {'markdown','html'}:
            raise ValueError('both real table formats required')
    if require_review:
        unsigned = {k:v for k,v in manifest.items() if k!='frozen_manifest_fingerprint'}
        if manifest.get('status')!='frozen' or manifest.get('frozen_manifest_fingerprint')!=fingerprint(unsigned):
            raise ValueError('OWNER_REVIEW_REQUIRED: reviewed manifest must be frozen')


def same_pool(baseline, variant):
    if len(set(baseline))!=len(baseline) or len(set(variant))!=len(variant) or set(baseline)!=set(variant):
        raise ValueError('invalid variant: missing/extra/duplicate candidate identity')


def _snapshot_items(snapshot):
    items = snapshot['items']
    unique(items,'chunk_id','snapshot chunk ID')
    if type(snapshot['candidate_limit']) is not int or snapshot['candidate_limit'] not in (8,16,32):
        raise ValueError('unvalidated candidate capacity')
    if len(items)>snapshot['candidate_limit'] or not snapshot.get('retrieval_run_id'):
        raise ValueError('invalid snapshot run/capacity')
    if [i['original_rank'] for i in items]!=list(range(1,len(items)+1)):
        raise ValueError('invalid original ranks')
    if any(type(i['hybrid_score']) not in (float,int) or not math.isfinite(i['hybrid_score']) for i in items):
        raise ValueError('invalid RRF score')
    return items


def baseline_ranking(snapshot, corpus):
    items = _snapshot_items(snapshot)
    if snapshot['corpus_fingerprint']!=corpus['corpus_fingerprint']:
        raise ValueError('dataset stale: snapshot corpus')
    chunks={c['chunk_id']:c for c in corpus['chunks']}
    docs={d['document_id']:d for d in corpus['documents']}
    for item in items:
        chunk=chunks.get(item['chunk_id']); document=docs.get(item['document_id'])
        if (chunk is None or document is None or document['deletion_status']!='normal'
                or chunk['content_fingerprint']!=item.get('content_fingerprint')
                or chunk['document_id']!=item['document_id']):
            raise ValueError('dataset stale: snapshot chunk')
    return [i['chunk_id'] for i in items]  # Never re-sort persisted RRF floats.


def rank_scores(snapshot, scores):
    items=_snapshot_items(snapshot)
    same_pool([i['chunk_id'] for i in items],[s['chunk_id'] for s in scores])
    expected={i['chunk_id']:i['original_rank'] for i in items}
    if any(expected[s['chunk_id']]!=s['original_rank'] or type(s['raw_score']) not in (int,float)
           or not math.isfinite(s['raw_score']) for s in scores):
        raise ValueError('invalid score identity/value')
    return [s['chunk_id'] for s in sorted(scores,key=lambda s:(-s['raw_score'],s['original_rank'],s['chunk_id']))]


def prefix_consistent(rankings):
    if set(rankings)!={8,16,32}:
        raise ValueError('all three capacities required')
    return all(rankings[k]==rankings[32][:k] for k in (8,16))


def aggregate(rows):
    def means(selected):
        quality=[r['metrics'] for r in selected if r['metrics']['answerable']]
        return {'quality_query_count':len(quality),'robustness_query_count':len(selected)-len(quality),
                **{key:sum(r[key] for r in quality)/len(quality) if quality else None for key in QUALITY_KEYS}}
    return {'overall':means(rows),
            'categories':{cat:means([r for r in rows if r['category']==cat]) for cat in (*CATEGORIES,'robustness')},
            'table_formats':{fmt:means([r for r in rows if r['category']=='structured_table' and r.get('table_format')==fmt])
                             for fmt in ('markdown','html')}}


def evaluate(manifest,corpus,snapshots,variants,*,split,phase='M4',owner_authorization=None,quality_only=False):
    if split=='final':
        auth=owner_authorization or {}
        if (phase!='M7' or auth.get('phase')!='M7' or not auth.get('owner_reference')
                or not manifest.get('frozen_manifest_fingerprint')
                or auth.get('manifest_fingerprint')!=manifest.get('frozen_manifest_fingerprint')):
            raise ValueError('final held-out requires explicit owner authorization at M7')
    if split=='selection' and phase!='M5':
        raise ValueError('Selection is reserved for M5')
    if split not in SPLITS:
        raise ValueError('invalid split')
    if quality_only and (split!='selection' or phase!='M5'):
        raise ValueError('quality_only is reserved for M5 Selection; robustness remains separate')
    validate_manifest(manifest,corpus,require_review=True)
    queries={q['query_id']:q for q in manifest['queries'] if q['split']==split
             and (not quality_only or q['category']!='robustness')}
    pairs=[(s['query_id'],s['candidate_limit']) for s in snapshots]
    if len(set(pairs))!=len(pairs) or {s['query_id'] for s in snapshots}!=set(queries):
        raise ValueError('snapshots must cover exactly the requested split')
    for capacity in {s['candidate_limit'] for s in snapshots}:
        if {s['query_id'] for s in snapshots if s['candidate_limit']==capacity} != set(queries):
            raise ValueError('capacity coverage must be identical across queries')
    expected_keys = {f'{query_id}:C{capacity}' for query_id,capacity in pairs}
    if set(variants) != expected_keys:
        raise ValueError('variant keys must cover exactly the snapshot pairs')
    reports=[]
    for snapshot in snapshots:
        q=queries[snapshot['query_id']]; baseline=baseline_ranking(snapshot,corpus)
        if snapshot.get('query_fingerprint') != content_hash(q['query']):
            raise ValueError('dataset stale: snapshot query identity')
        key=f"{q['query_id']}:C{snapshot['candidate_limit']}"
        scores=variants.get(key)
        if scores is None:
            raise ValueError('missing variant; partial evaluation forbidden')
        variant=rank_scores(snapshot,scores); same_pool(baseline,variant)
        gold={r['chunk_id']:r['grade'] for r in q['qrels']}
        reports.append({'query_id':q['query_id'],'candidate_limit':snapshot['candidate_limit'],
                        'category':q['category'],'table_format':q.get('table_format'),
                        'baseline':query_metrics(baseline,gold,baseline),
                        'variant':query_metrics(variant,gold,baseline)})
    # Capacities remain separate: never average C8/C16/C32 into one quality score.
    return {'split':split,'corpus_fingerprint':corpus['corpus_fingerprint'],'queries':reports,
            'by_capacity':{str(cap):{name:aggregate([dict(r,metrics=r[name]) for r in reports if r['candidate_limit']==cap])
                                    for name in ('baseline','variant')}
                           for cap in sorted({r['candidate_limit'] for r in reports})}}


def main():
    import argparse
    import json
    from pathlib import Path
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split',choices=SPLITS,required=True)
    parser.add_argument('--manifest',required=True); parser.add_argument('--corpus',required=True)
    parser.add_argument('--snapshots',required=True); parser.add_argument('--variants',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    # Deliberately no CLI option that authorizes held-out or parameter selection.
    if args.split!='development':
        parser.error('final held-out requires explicit owner authorization at M7; selection is reserved for M5')
    read=lambda p:json.loads(Path(p).read_text(encoding='utf-8'))
    result=evaluate(read(args.manifest),read(args.corpus),read(args.snapshots),read(args.variants),split=args.split)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    main()
