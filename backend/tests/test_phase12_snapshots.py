from types import SimpleNamespace

import pytest

from tests.test_reranker_evaluation import fixtures


def test_prefix_capture_uses_real_order_three_calls_and_no_bodies():
    from tests.phase12_local.candidate_snapshots import probe_prefix
    manifest, corpus, snapshot = fixtures()
    calls = []
    def retrieve(query, limit):
        calls.append((query, limit))
        return SimpleNamespace(items=[SimpleNamespace(**dict(i, content='private body')) for i in snapshot['items']])
    # The supplied corpus must agree with freshly fetched content.
    from tests.phase12_local.corpus_audit import content_hash
    for chunk in corpus['chunks']:
        chunk['content_fingerprint'] = content_hash('private body')
    report = probe_prefix([manifest['queries'][0]], retrieve, corpus, 'run')
    assert [c[1] for c in calls] == [8, 16, 32]
    assert report['prefix_consistent'] is True
    assert report['strategy'] == 'one_C32_snapshot_prefixes'
    assert len(report['snapshots']) == 3
    assert 'private body' not in str(report)


def test_prefix_drift_preserves_independent_snapshots():
    from tests.phase12_local.candidate_snapshots import probe_prefix
    from tests.phase12_local.corpus_audit import content_hash
    manifest, corpus, snapshot = fixtures()
    for c in corpus['chunks']: c['content_fingerprint'] = content_hash('body')
    def retrieve(query, limit):
        items = snapshot['items'] if limit == 32 else snapshot['items'][::-1]
        return SimpleNamespace(items=[SimpleNamespace(**dict(i, content='body')) for i in items])
    result = probe_prefix([manifest['queries'][0]], retrieve, corpus, 'run')
    assert result['prefix_consistent'] is False
    assert result['strategy'] == 'independent_snapshot_per_C'


@pytest.mark.parametrize('split', ['selection', 'final'])
def test_prefix_gate_before_any_retrieval(split):
    from tests.phase12_local.candidate_snapshots import probe_prefix
    def forbidden(*args): raise AssertionError('retrieval must not run')
    with pytest.raises(ValueError, match='Development only'):
        probe_prefix([{'split': split}], forbidden, {}, 'run')


def test_changed_live_content_rejected():
    from tests.phase12_local.candidate_snapshots import probe_prefix
    m, c, s = fixtures()
    def retrieve(query, limit):
        return SimpleNamespace(items=[SimpleNamespace(**dict(i,content='changed')) for i in s['items']])
    with pytest.raises(ValueError, match='stale'):
        probe_prefix([m['queries'][0]], retrieve, c, 'run')
