"""Read-only corpus evidence helpers. No connections, model imports, or writes at import."""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json

SCHEMA_VERSION = 'phase12-golden-v1'
LONG_TEXT_MIN_CHARS = 500  # Descriptive audit slice, not a tokenizer safety budget.


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def fingerprint(value) -> str:
    return content_hash(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')))


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.depth = 0; self.rows = 0; self.cells = 0; self.found = False

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            if not self.depth:
                self.rows = self.cells = 0
            self.depth += 1
        elif self.depth:
            self.rows += tag == 'tr'
            self.cells += tag in ('td', 'th')

    def handle_endtag(self, tag):
        if tag == 'table' and self.depth:
            self.depth -= 1
            if not self.depth and self.rows >= 2 and self.cells >= 4:
                self.found = True


def table_formats(content: str) -> list[str]:
    # Reuse the installed Markdown grammar; fenced/inline examples are not tables.
    from markdown_it import MarkdownIt
    tokens = MarkdownIt('commonmark').enable('table').parse(content)
    result = ['markdown'] if any(t.type == 'table_open' for t in tokens) else []
    parser = _TableParser()
    for token in tokens:
        if token.type == 'html_block':
            parser.feed(token.content)
        elif token.type == 'inline':
            for child in token.children or ():
                if child.type == 'html_inline':
                    parser.feed(child.content)
    if parser.found:
        result.append('html')
    return result


def eligibility(document: dict, chunks: list[dict], index: dict[str, dict]) -> list[str]:
    reasons = []
    if document.get('deletion_status') != 'normal':
        reasons.append('document_not_normal')
    if not chunks:
        reasons.append('no_persisted_chunks')
    for chunk in chunks:
        if (chunk.get('document_id') != document['document_id']
                or chunk.get('embedding_status') != 'embedded'
                or chunk.get('embedding_model') != 'Qwen3-Embedding-0.6B'
                or chunk.get('embedding_dim') != 1024
                or chunk.get('actual_embedding_dim') != 1024
                or chunk.get('has_embedding') is not True):
            reasons.append('embedding_incomplete')
        hit = index.get(chunk['chunk_id'], {})
        if (not hit.get('lexical_present') or not hit.get('vector_present')
                or any(hit.get(k) != chunk.get(k) for k in
                       ('document_id', 'content_fingerprint', 'embedding_model', 'embedding_dim', 'embedding_status'))):
            reasons.append('index_missing_or_stale')
    return sorted(set(reasons))


def _unique(records: list[dict], key: str) -> None:
    ids = [row[key] for row in records]
    if len(ids) != len(set(ids)):
        raise ValueError(f'duplicate {key}')


def corpus_identity(documents: list[dict], chunks: list[dict], index_identity: dict, commit: str) -> str:
    _unique(documents, 'document_id'); _unique(chunks, 'chunk_id')
    # Bodies/vectors are represented by their fingerprints and declared identity.
    safe_chunks = [{k:v for k,v in c.items() if k not in ('content','embedding','table_formats','char_count')}
                   for c in chunks]
    return fingerprint({'schema_version': SCHEMA_VERSION, 'code_commit': commit,
                        'documents': sorted(documents, key=lambda d:d['document_id']),
                        'chunks': sorted(safe_chunks, key=lambda c:c['chunk_id']),
                        'opensearch': index_identity})


def catalog(documents: list[dict], chunks: list[dict], index_identity: dict, commit: str) -> dict:
    digest = corpus_identity(documents, chunks, index_identity, commit)
    safe = []
    for c in chunks:
        item = {k:v for k,v in c.items() if k not in ('content','embedding')}
        item['table_formats'] = table_formats(c['content'])
        item['char_count'] = len(c['content'])
        safe.append(item)
    return {'schema_version': SCHEMA_VERSION, 'code_commit': commit,
            'corpus_fingerprint': digest, 'documents': documents, 'chunks': safe,
            'opensearch': index_identity}
