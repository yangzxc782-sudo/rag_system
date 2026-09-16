import importlib
import sys
from types import SimpleNamespace

import pytest


def module():
    return importlib.import_module('tests.phase12_local.token_length_audit')


def test_tokenizer_local_only_no_model(tmp_path, monkeypatch):
    calls=[]; sentinel=object()
    def load(path, **kwargs):
        calls.append((path,kwargs)); return sentinel
    monkeypatch.setitem(sys.modules,'transformers',SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=load)))
    assert module().load_tokenizer(tmp_path) is sentinel
    assert calls==[(str(tmp_path),{'local_files_only':True,'trust_remote_code':False,'use_fast':True})]


def test_missing_path_does_not_download(tmp_path):
    with pytest.raises(ValueError, match='local tokenizer'):
        module().load_tokenizer(tmp_path/'missing')


def test_percentiles_and_coverage():
    r=module().summarize([0,1024,1025,2048,2049,4096,4097])
    assert r['median']==2048
    assert r['p95']==pytest.approx(4096.7)
    assert r['p99']==pytest.approx(4096.94)
    assert [r['coverage'][str(n)]['truncated_count'] for n in (1024,2048,4096)]==[5,3,1]
    assert r['coverage']['1024']['fully_covered_count']==2
    assert r['coverage']['4096']['truncated_ratio']==pytest.approx(1/7)


def test_empty_summary():
    r=module().summarize([])
    assert r['count']==0 and r['p95'] is None
    assert r['coverage']['1024']['truncated_ratio'] is None


def test_only_approved_length_candidates():
    assert module().MAX_LENGTH_CANDIDATES==(1024,2048,4096)


def test_token_only_audit_separate_slices_and_pair_budget():
    class Tokenizer:
        def encode(self,text,**kwargs):
            assert kwargs=={'add_special_tokens':False,'truncation':False}
            return list(range(len(text)))
        def num_special_tokens_to_add(self,pair):
            assert pair is True; return 4
    chunks=[{'chunk_id':'m','content':'m'*1100,'table_formats':['markdown']},
            {'chunk_id':'h','content':'h'*2100,'table_formats':['html']},
            {'chunk_id':'p','content':'p'*600,'table_formats':[]},
            {'chunk_id':'s','content':'short','table_formats':[]}]
    r=module().audit_tokens(chunks,Tokenizer())
    assert r['pair_special_tokens']==4 and r['model_forward_count']==0
    assert r['slices']['markdown']['count']==1
    assert r['slices']['html']['count']==1
    assert r['slices']['long_paragraph']['count']==1
    assert r['slices']['ordinary_text']['count']==1
    assert r['slices']['overall']['count']==4
    assert r['coverage_basis']=='chunk_only_excluding_query_and_special_tokens'
    assert 'content' not in r['chunks'][0]
