import importlib
import math

import pytest


def metrics(ranking, gold, candidates=None):
    return importlib.import_module('tests.phase12_local.metrics').query_metrics(
        ranking, gold, ranking if candidates is None else candidates)


def test_rank1_grade2_hand_calculated():
    r=metrics(['a'],{'a':2})
    assert {k:r[k] for k in ('hit_rate_at_1','hit_rate_at_3','recall_at_8','mrr_at_8','ndcg_at_8')} == dict.fromkeys(
        ('hit_rate_at_1','hit_rate_at_3','recall_at_8','mrr_at_8','ndcg_at_8'),1.0)
    assert r['dcg_at_8']==3 and r['idcg_at_8']==3


def test_rank3_first_relevant():
    r=metrics(['x','y','a'],{'a':2})
    assert r['hit_rate_at_1']==0 and r['hit_rate_at_3']==1
    assert r['mrr_at_8']==pytest.approx(1/3)
    assert r['ndcg_at_8']==.5


def test_mixed_grades_and_multiple_gold():
    r=metrics(['b','a'],{'a':2,'b':1,'c':1})
    assert r['recall_at_8']==pytest.approx(2/3)
    assert r['dcg_at_8']==pytest.approx(1+3/math.log2(3))
    assert r['idcg_at_8']==pytest.approx(3+1/math.log2(3)+1/math.log2(4))


def test_gold_outside_candidate_remains_in_denominator():
    r=metrics(['a','x'],{'a':1,'missing':2})
    assert r['recall_at_8']==.5 and r['candidate_coverage']==.5
    assert r['recall_at_8_ceiling']==.5 and r['idcg_at_8']>3


def test_relevant_outside_top8_and_coverage_ceiling():
    ids=[str(i) for i in range(10)]
    r=metrics(ids,{'9':2})
    assert r['recall_at_8']==r['mrr_at_8']==r['ndcg_at_8']==0
    assert r['candidate_coverage']==1
    r=metrics(ids,dict.fromkeys(ids,1))
    assert r['recall_at_8_ceiling']==.8


def test_empty_results_for_answerable_query():
    r=metrics([],{'a':2})
    assert r['answerable'] is True
    assert r['recall_at_8']==r['candidate_coverage']==r['ndcg_at_8']==0


@pytest.mark.parametrize('gold',[{}, {'a':0}])
def test_no_answer_separate_robustness(gold):
    r=metrics(['a'],gold)
    assert r['answerable'] is False and r['idcg_at_8']==0
    for key in ('recall_at_8','mrr_at_8','ndcg_at_8','hit_rate_at_1','hit_rate_at_3'):
        assert r[key] is None


@pytest.mark.parametrize('ranking,candidates',[(['a','a'],['a','a']),(['a'],['a','a']),(['unknown'],['a'])])
def test_invalid_snapshot_identity(ranking,candidates):
    with pytest.raises(ValueError): metrics(ranking,{'a':1},candidates)


@pytest.mark.parametrize('grade',[-1,3,True,1.5])
def test_grade_mapping_strict(grade):
    with pytest.raises(ValueError): metrics(['a'],{'a':grade})
