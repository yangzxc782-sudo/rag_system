"""Token-only evidence; never constructs a model or initializes CUDA."""
from __future__ import annotations

import math
from pathlib import Path

from tests.phase12_local.corpus_audit import LONG_TEXT_MIN_CHARS

MAX_LENGTH_CANDIDATES = (1024, 2048, 4096)


def load_tokenizer(path):
    path = Path(path)
    if not path.is_dir():
        raise ValueError('existing local tokenizer directory required')
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(str(path), local_files_only=True,
                                         trust_remote_code=False, use_fast=True)


def percentile(values: list[int], p: float):
    if not values:
        return None
    ordered = sorted(values); at = (len(ordered)-1)*p
    low, high = math.floor(at), math.ceil(at)
    return ordered[low]+(ordered[high]-ordered[low])*(at-low)


def summarize(lengths: list[int]) -> dict:
    if any(type(n) is not int or n < 0 for n in lengths):
        raise ValueError('invalid token count')
    n = len(lengths)
    return {'count': n, 'min': min(lengths) if n else None,
            'median': percentile(lengths, .5), 'p75': percentile(lengths, .75),
            'p90': percentile(lengths, .9), 'p95': percentile(lengths, .95),
            'p99': percentile(lengths, .99), 'max': max(lengths) if n else None,
            'coverage': {str(limit): {'fully_covered_count': sum(x<=limit for x in lengths),
                                     'truncated_count': sum(x>limit for x in lengths),
                                     'truncated_ratio': sum(x>limit for x in lengths)/n if n else None}
                         for limit in MAX_LENGTH_CANDIDATES}}


def audit_tokens(chunks: list[dict], tokenizer) -> dict:
    slices = {key:[] for key in ('ordinary_text','long_paragraph','markdown','html','overall')}
    records = []
    for chunk in chunks:
        n = len(tokenizer.encode(chunk['content'], add_special_tokens=False, truncation=False))
        formats = chunk['table_formats']
        labels = list(formats) if formats else [
            'long_paragraph' if len(chunk['content']) >= LONG_TEXT_MIN_CHARS else 'ordinary_text']
        for label in (*labels, 'overall'):
            slices[label].append(n)
        records.append({'chunk_id':chunk['chunk_id'], 'token_count':n, 'slices':labels})
    return {'max_length_candidates':list(MAX_LENGTH_CANDIDATES),
            'coverage_basis':'chunk_only_excluding_query_and_special_tokens',
            'pair_special_tokens':tokenizer.num_special_tokens_to_add(pair=True),
            'model_forward_count':0, 'chunks':records,
            'slices':{key:summarize(values) for key,values in slices.items()}}
