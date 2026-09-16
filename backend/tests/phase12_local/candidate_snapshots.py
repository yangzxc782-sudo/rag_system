"""M4 Development-only prefix audit. Inject the existing production Hybrid call.

The three calls establish prefix evidence, never two independently retrieved arms
of a ranking comparison. Retain every original C snapshot if any prefix differs.
"""
from tests.phase12_local.corpus_audit import content_hash, fingerprint
from tests.phase12_local.evaluation import baseline_ranking, unique


def probe_prefix(queries, retrieve, corpus, run_id):
    if not queries or any(q['split'] != 'development' for q in queries):
        raise ValueError('M4 prefix audit is Development only')
    unique(queries, 'query_id', 'query ID')
    chunks = {c['chunk_id']: c for c in corpus['chunks']}
    snapshots = []
    checks = []
    for q in queries:
        by_capacity = {}
        for capacity in (8, 16, 32):
            result = retrieve(q['query'], capacity)
            items = []
            for rank, item in enumerate(result.items, 1):
                chunk = chunks.get(item.chunk_id)
                if chunk is None or chunk['content_fingerprint'] != content_hash(item.content):
                    raise ValueError('dataset stale: live retrieved content changed')
                items.append({
                    'chunk_id': item.chunk_id, 'document_id': item.document_id,
                    'original_rank': rank, 'hybrid_score': item.hybrid_score,
                    'keyword_rank': item.keyword_rank, 'vector_rank': item.vector_rank,
                    'content_fingerprint': chunk['content_fingerprint'],
                })
            snapshot = {
                'query_id': q['query_id'], 'query_fingerprint': content_hash(q['query']),
                'candidate_limit': capacity, 'retrieval_run_id': run_id,
                'corpus_fingerprint': corpus['corpus_fingerprint'], 'items': items,
            }
            baseline_ranking(snapshot, corpus)
            snapshots.append(snapshot)
            by_capacity[capacity] = items
        checks.append({'query_id': q['query_id'],
                       'C8_matches_C32': by_capacity[8] == by_capacity[32][:8],
                       'C16_matches_C32': by_capacity[16] == by_capacity[32][:16]})
    passed = all(c['C8_matches_C32'] and c['C16_matches_C32'] for c in checks)
    return {'purpose': 'prefix audit, no quality metrics or BGE inference',
            'retrieval_run_id': run_id, 'query_count': len(queries),
            'hybrid_call_count': 3 * len(queries), 'prefix_consistent': passed,
            'strategy': 'one_C32_snapshot_prefixes' if passed else 'independent_snapshot_per_C',
            'corpus_fingerprint': corpus['corpus_fingerprint'],
            'checks': checks, 'snapshots': snapshots,
            'snapshot_fingerprint': fingerprint(snapshots)}
