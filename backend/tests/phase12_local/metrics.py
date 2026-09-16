"""Pure ranking metrics; no retrieval, model, or I/O dependencies."""
from __future__ import annotations

import math

QUALITY_KEYS = ('hit_rate_at_1', 'hit_rate_at_3', 'recall_at_8', 'mrr_at_8', 'ndcg_at_8',
                'candidate_coverage', 'recall_at_8_ceiling')


def query_metrics(ranking: list[str], gold: dict[str, int], candidates: list[str]) -> dict:
    if len(set(ranking)) != len(ranking) or len(set(candidates)) != len(candidates):
        raise ValueError('duplicate candidate identity')
    if set(ranking) != set(candidates):
        raise ValueError('ranking must be a permutation of the same candidate pool')
    if any(type(grade) is not int or grade not in (0, 1, 2) for grade in gold.values()):
        raise ValueError('invalid relevance grade')
    relevant = {cid for cid, grade in gold.items() if grade > 0}
    def dcg(grades):
        return sum((2**grade-1)/math.log2(rank+1) for rank, grade in enumerate(grades, 1))
    ideal = dcg(sorted(gold.values(), reverse=True)[:8])
    actual = dcg([gold.get(cid, 0) for cid in ranking[:8]])
    result = {'answerable': bool(relevant), 'dcg_at_8': actual, 'idcg_at_8': ideal}
    if not relevant:
        return dict(result, **dict.fromkeys(QUALITY_KEYS, None))
    captured = relevant.intersection(candidates)
    result.update(
        hit_rate_at_1=float(bool(relevant.intersection(ranking[:1]))),
        hit_rate_at_3=float(bool(relevant.intersection(ranking[:3]))),
        recall_at_8=len(relevant.intersection(ranking[:8]))/len(relevant),
        mrr_at_8=next((1/r for r, cid in enumerate(ranking[:8], 1) if cid in relevant), 0.0),
        ndcg_at_8=actual/ideal,
        candidate_coverage=len(captured)/len(relevant),
        recall_at_8_ceiling=min(8, len(captured))/len(relevant),
    )
    return result
