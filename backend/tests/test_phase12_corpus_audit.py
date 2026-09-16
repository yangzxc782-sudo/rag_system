"""M4 audit contracts: synthetic unit inputs, no live database or model."""
import copy
import importlib

import pytest


def module():
    return importlib.import_module('tests.phase12_local.corpus_audit')


@pytest.mark.parametrize('content,expected', [
    ('| Grade | MPa |\n|---|---:|\n| A | 200 |', ['markdown']),
    ('<table><tr><th>A</th><th>B</th></tr><tr><td>x</td><td>1</td></tr></table>', ['html']),
    ('<p>normal HTML</p>', []), ('a | b without a table', []),
    ('`<table>`', []), ('<table>not rows</table>', []),
])
def test_structured_table_detection(content, expected):
    assert module().table_formats(content) == expected


def records():
    d = {'document_id': 'd', 'deletion_status': 'normal'}
    c = {'chunk_id': 'c', 'document_id': 'd', 'content': 'evidence',
         'content_fingerprint': module().content_hash('evidence'),
         'embedding_status': 'embedded', 'embedding_model': 'Qwen3-Embedding-0.6B',
         'embedding_dim': 1024, 'actual_embedding_dim': 1024, 'has_embedding': True}
    i = dict(c, lexical_present=True, vector_present=True)
    return d, c, i


def test_eligible_document_requires_matching_complete_index():
    d, c, i = records()
    assert module().eligibility(d, [c], {'c': i}) == []


@pytest.mark.parametrize('field,value', [('deletion_status','deleting'), ('deletion_status','delete_failed')])
def test_deleted_document_ineligible(field, value):
    d, c, i = records(); d[field] = value
    assert module().eligibility(d, [c], {'c':i})


@pytest.mark.parametrize('field,value', [
    ('embedding_status','not_started'), ('embedding_model','other'), ('embedding_dim',768),
    ('actual_embedding_dim',768), ('has_embedding',False),
])
def test_incomplete_embedding_ineligible(field, value):
    d, c, i = records(); c[field] = value
    assert module().eligibility(d, [c], {'c':i})


@pytest.mark.parametrize('field,value', [
    ('lexical_present',False), ('vector_present',False), ('document_id','unknown'),
    ('content_fingerprint','changed'), ('embedding_model','other'), ('embedding_dim',768),
])
def test_index_drift_ineligible(field,value):
    d, c, i = records(); i[field] = value
    assert module().eligibility(d, [c], {'c':i})


def test_missing_and_duplicate_chunk_rejected():
    d, c, i = records()
    assert module().eligibility(d, [c], {})
    assert module().eligibility(d, [], {})
    with pytest.raises(ValueError, match='duplicate'):
        module().corpus_identity([d], [c,c], {}, 'head')


def test_corpus_hash_order_independent_and_sensitive_to_identity():
    d,c,i = records(); other=dict(c, chunk_id='other')
    first=module().corpus_identity([d],[c,other],{'index':'v1'},'head')
    assert first == module().corpus_identity([d],[other,c],{'index':'v1'},'head')
    assert first != module().corpus_identity([d],[c,other],{'index':'v2'},'head')
    c['content_fingerprint']='changed'
    assert first != module().corpus_identity([d],[c,other],{'index':'v1'},'head')


def test_safe_catalog_omits_body_and_vectors():
    d,c,i=records(); c['embedding']=[1.0]*1024
    safe=module().catalog([d],[c],{},'head')
    assert 'content' not in safe['chunks'][0] and 'embedding' not in safe['chunks'][0]
    assert safe['chunks'][0]['content_fingerprint']==c['content_fingerprint']
