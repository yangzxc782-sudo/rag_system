"""Request-level observations around production calls, without reimplementing them.

M2 helpers supply fake dependencies; M3 observes CUDA. Neither links a live Hybrid
filter to RAG/Context/Prompt/Graph in a single request. This small adapter does so.
It stores only identities/counts/timings, never query, passage, prompt, or secrets.
"""
from contextlib import ExitStack
from dataclasses import asdict
import re
from time import perf_counter
from unittest.mock import patch

from app.services import hybrid_search, rag
from tests.phase12_local.bge_probe import ProbeDisabled, run_gated


def run_real_gated(operation, *, environ=None):
    try:
        return run_gated(operation, environ=environ)
    except ProbeDisabled:
        return {'status': 'disabled', 'real_load_count': 0}


class RequestTrace:
    def __init__(self, service=None, graph=None):
        self.service, self.graph = service, graph
        self.hybrid_limits, self.hybrid_ids, self.reranker_ids = [], [], []
        self.raw_ids, self.safe_ids = [], []
        self.final_ids, self.context_ids, self.prompt_ids = [], [], []
        self.graph_calls, self.expected_refs, self.expected_provenance = [], [], []
        self.events = []
        self.fallback_reason = None
        self.applied = False
        self.filter_observed = False
        self.hybrid_ms = self.rerank_ms = self.request_ms = 0.0
        self.original_scores = {}

    def __enter__(self):
        self.stack = ExitStack()
        self.started = perf_counter()
        def wrap(obj, name, observer):
            original = getattr(obj, name)
            self.stack.enter_context(patch.object(obj, name, side_effect=lambda *a, **kw: observer(original, *a, **kw)))
        wrap(hybrid_search, 'hybrid_search_chunks', self._hybrid)
        wrap(hybrid_search, '_filter_normal_document_hits', self._filter)
        wrap(rag, 'optional_rerank_chunks', self._rerank_outcome)
        wrap(rag, 'build_context', self._context)
        wrap(rag, 'build_prompt', self._prompt)
        if self.service is not None:
            wrap(self.service, 'rerank', self._rerank)
        if self.graph is not None:
            wrap(self.graph, 'retrieve', self._graph)
        return self

    def __exit__(self, *exc):
        self.request_ms = (perf_counter() - self.started) * 1000
        return self.stack.__exit__(*exc)

    def _hybrid(self, operation, *args, **kwargs):
        self.hybrid_limits.append(kwargs['limit'])
        start = perf_counter()
        result = operation(*args, **kwargs)
        self.hybrid_ms = (perf_counter() - start) * 1000
        self.hybrid_ids = [s.chunk_id for s in result.items]
        self.original_scores = {s.chunk_id: s.hybrid_score for s in result.items}
        self.events.append('hybrid_return')
        return result

    def _filter(self, operation, keyword, vector, **kwargs):
        self.filter_observed = True
        self.raw_ids = list(dict.fromkeys(h.chunk_id for h in [*keyword, *vector]))
        result = operation(keyword, vector, **kwargs)
        self.safe_ids = list(dict.fromkeys(h.chunk_id for hits in result for h in hits))
        self.events.append('deletion_filter')
        return result

    def _rerank(self, operation, request):
        self.reranker_ids = [c.chunk_id for c in request.candidates]
        self.events.append('reranker_input')
        return operation(request)

    def _rerank_outcome(self, operation, *args, **kwargs):
        start = perf_counter()
        result = operation(*args, **kwargs)
        self.rerank_ms = (perf_counter() - start) * 1000
        self.final_ids = [s.chunk_id for s in result.search_result.items]
        self.fallback_reason, self.applied = result.fallback_reason, result.applied
        return result

    def _context(self, operation, *args, **kwargs):
        result = operation(*args, **kwargs)
        self.context_ids = [c.chunk_id for c in result.chunks]
        refs, provenance = rag.extract_graph_refs(result)
        self.expected_refs = [asdict(r) for r in refs]
        self.expected_provenance = [asdict(p) for p in provenance]
        return result

    def _prompt(self, operation, *args, **kwargs):
        result = operation(*args, **kwargs)
        self.prompt_ids = re.findall(r'^chunk_id: ([^\r\n]+)$', result.user_prompt, flags=re.MULTILINE)
        return result

    def _graph(self, operation, refs, *, provenance=()):
        self.graph_calls.append({'refs': [asdict(r) for r in refs],
                                 'provenance': [asdict(p) for p in provenance]})
        return operation(refs, provenance=provenance)

    def verify(self, answer):
        assert len(self.hybrid_limits) == 1, 'once Hybrid'
        assert self.context_ids == self.prompt_ids, 'prompt/context order'
        assert self.context_ids == [c.chunk_id for c in answer.citations], 'citation/context order'
        assert self.final_ids == [s.chunk_id for s in answer.retrieval.items], 'final retrieval order'
        assert self.context_ids == self.final_ids[:len(self.context_ids)], 'context final prefix'
        assert len(self.graph_calls) <= 1, 'once Graph retrieval'
        if self.graph_calls:
            assert self.graph_calls == [{'refs': self.expected_refs, 'provenance': self.expected_provenance}], 'Graph provenance'
        if self.filter_observed:
            assert set(self.reranker_ids) <= set(self.safe_ids) <= set(self.raw_ids), 'deletion before model'
        assert all(s.hybrid_score == self.original_scores[s.chunk_id] for s in answer.retrieval.items), 'hybrid_score drift'

    def record(self):
        names = ('hybrid_limits', 'hybrid_ids', 'raw_ids', 'safe_ids', 'reranker_ids',
                 'final_ids', 'context_ids', 'prompt_ids', 'graph_calls', 'events',
                 'fallback_reason', 'applied', 'filter_observed', 'hybrid_ms', 'rerank_ms', 'request_ms')
        return {name: getattr(self, name) for name in names}
