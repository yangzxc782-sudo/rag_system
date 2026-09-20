"""Opt-in, read-only M6 runner. Uses production algorithms and existing M0/M3/M4 helpers.

No dataset questions/qrels are loaded. The six queries below are dedicated wiring
inputs, not a quality evaluation. Invoke as a module with the existing probe gate.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from tests.phase12_integration.observation import RequestTrace, run_real_gated
from tests.phase12_local.bge_probe import summarize_latencies, sanitize_report


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / 'backend/tests/fixtures/phase12'
GOLDEN_SHA = 'dadaceb59b3437d35f8356399f0dc70c6f60ad52e51433d989a42e418c4f6289'
GOLDEN_FINGERPRINT = 'cdbe63039b4d9a674b2fca3ff7ab1bb3fe941c88c06bec557d165f21b4294c2a'
CORPUS_FINGERPRINT = 'c4e5ffcc12e119e8e3b36d3750ed80e1a0945cc4dfcb400cd8eb5b21762412c1'
QUERIES = (
    ('text', '铸型工艺中如何控制砂型质量？'),
    ('numeric', 'QT400-18 的力学性能要求是什么？'),
    ('table', '铸铁试样抗拉强度表中有哪些数值？'),
    ('long_paragraph', '球墨铸铁热处理工艺的主要步骤是什么？'),
    ('exact_identifier', 'GB/T 9439 标准适用什么材料？'),
    ('paraphrase_observation', '怎样让铸件更不容易出现孔洞？'),
)
# Dedicated Graph contract query taken from live corpus metadata/content, never Gold.
GRAPH_QUERY = '表格 T-P9-1 中 WCB 的室温拉伸性能、抗拉强度和伸长率要求是什么？'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_performance(warm, incremental, peak_mib):
    rerank = summarize_latencies(warm)
    # Raw signed paired deltas are retained in evidence. Negative network jitter
    # contributes zero overhead; never remove a slow positive observation.
    delta = summarize_latencies([max(0.0, v) for v in incremental])
    assert rerank['p95_ms'] <= 2000, 'warm rerank SLO'
    assert delta['p95_ms'] <= 2200, 'retrieval incremental SLO'
    assert peak_mib <= 6500, 'GPU coexist SLO'
    return {'status': 'PASS', 'warm_rerank': rerank, 'retrieval_incremental': delta,
            'device_sampled_peak_mib': peak_mib}


def run(output, *, recover_llm_once=False):
    return run_real_gated(lambda: _real(Path(output), recover_llm_once=recover_llm_once))


def llm_metadata(base, active):
    from app.llm.configuration import validate_active_llm_configuration
    fields = ('llm_provider', 'llm_remote_model', 'llm_remote_base_url',
              'llm_remote_api_key', 'llm_remote_timeout_seconds')
    assert all(getattr(base, k) == getattr(active, k) for k in fields), 'LLM config drift'
    metadata = validate_active_llm_configuration(active)
    assert (metadata.provider, metadata.model) == ('api', 'gpt-4o-mini'), 'Owner LLM metadata mismatch'
    url = urlsplit(active.llm_remote_base_url.strip())
    return {'provider': metadata.provider, 'model': metadata.model,
            'base_url_host': url.hostname, 'base_url_path': url.path,
            'base_url_matches_settings': True, 'remote_api_key': 'SET',
            'timeout_seconds': active.llm_remote_timeout_seconds,
            'timeout_matches_settings': True}


def llm_generate_preflight(provider, report):
    from app.llm.messages import LLMMessage, LLMTextContentPart
    from app.llm.provider import LLMGenerateRequest
    from app.core.errors import BusinessError
    row = report['llm_preflight'] = {'status': 'RUNNING', 'generate_call_count': 1}
    started = perf_counter()
    try:
        result = provider.generate(LLMGenerateRequest(messages=(LLMMessage(role='user',
            content=(LLMTextContentPart(text='Reply with PROJECT_API_OK only.'),)),)))
        assert result.text.strip() and (result.provider, result.model) == ('api', 'gpt-4o-mini')
        row.update(status='PASS', response_nonempty=True, provider=result.provider, model=result.model)
    except Exception as exc:
        row.update(status='FAIL', exception_class=type(exc).__name__)
        if isinstance(exc, BusinessError):
            row['normalized_error_code'] = exc.code
            error_type = (exc.detail or {}).get('error_type')
            if isinstance(error_type, str) and error_type.isidentifier():
                row['upstream_exception_class'] = error_type
        raise
    finally:
        row['latency_ms'] = (perf_counter() - started) * 1000


def controlled_llm_recovery(report, preflight, rag_case):
    """Exactly one preflight and at most one original RAG attempt; no retry loop."""
    from app.core.errors import BusinessError, LLM_UNAVAILABLE
    report.update(standalone_owner_project_provider_probe='PASS (Owner reported)',
                  rag_retry_status='NOT_RUN', recovery_status='PENDING')
    try:
        preflight()
        report['rag_retry_status'] = 'RUNNING'
        rag_case()
    except Exception as exc:
        if report['rag_retry_status'] == 'RUNNING':
            report['rag_retry_status'] = 'FAIL'
        report['recovery_status'] = ('M6_REAL_RUNNER_LLM_CONNECTION_REPRODUCIBLE'
            if isinstance(exc, BusinessError) and exc.code == LLM_UNAVAILABLE else 'RECOVERY_STOPPED')
        raise
    report.update(rag_retry_status='PASS',
                  recovery_status='TRANSIENT_REAL_LLM_CONNECTION_FAILURE_RECOVERED')


def record_failure(report, stage, exc):
    from app.core.errors import BusinessError
    report.update(status='STOP', failure_stage=stage, error_type=type(exc).__name__)
    for row in report['requests']:
        if row['status'] == 'RUNNING':
            row['status'] = 'STOP'
    if isinstance(exc, BusinessError):
        report['business_error_code'] = exc.code
        error_type = (exc.detail or {}).get('error_type')
        if isinstance(error_type, str) and error_type.isidentifier():
            report['upstream_exception_class'] = error_type


def _real(output, *, recover_llm_once=False):
    os.environ['HF_HUB_OFFLINE'] = os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    logging.disable(logging.CRITICAL)  # Do not persist provider URLs/errors/prompts.
    from app.core.config import get_settings
    from app.graph.repository import Neo4jRepository
    from app.services.graph_retrieval import GraphRetrievalService
    from app.llm.provider import build_llm_provider, clear_llm_provider_cache
    from app.retrieval.embeddings import get_embedding_provider
    from app.retrieval import local_cross_encoder as runtime
    from app.retrieval.reranker import RerankerConfig
    from app.search_engine.client import get_search_engine_client
    from app.services import hybrid_search, rag, reranking
    from app.schemas.rag import RagAskData
    from tests.phase12_local.selected_profile import (
        read_selected_profile, frozen_settings, verify_local_model, assert_frozen_settings,
    )
    from tests.phase12_local.finalization import verify_live_corpus, read_only_session
    from tests.phase12_local.bge_probe import gpu_snapshot
    from tests.phase12_local.bge_smoke import DeviceSampler, Observation, SmokeLLM
    sys.path.insert(0, str(ROOT / 'backend/tests'))
    from integration.phase10_support import load_phase10_integration_settings

    if output.exists():
        raise FileExistsError('M6 evidence is append-only; choose a new output')
    if recover_llm_once:
        for prior in output.parent.glob('*.json'):
            if json.loads(prior.read_text(encoding='utf-8')).get('owner_recovery_attempt') == 1:
                raise RuntimeError('Owner recovery allowance already consumed; no third attempt')
    output.parent.mkdir(parents=True, exist_ok=True)
    selected = read_selected_profile()
    report = {'status': 'RUNNING', 'm5_commit': 'f6c1c01200ad859cd7555846a96da4b886333205',
        'start_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'profile_id': selected['profile_id'], 'profile_fingerprint': selected['profile_fingerprint'],
        'identity': selected['identity'], 'golden_fingerprint': GOLDEN_FINGERPRINT,
        'selection_policy': selected['selection_policy'], 'known_paraphrase_risk': selected['known_paraphrase_risk'],
        'final_run_count': 0, 'selection_run_count': 0, 'requests': [], 'pairs': [],
        'query_source': 'six dedicated integration queries in runner; no Golden questions loaded',
        'code_sha256': {str(p.relative_to(ROOT)).replace('\\', '/'): sha(p)
            for p in [Path(__file__), Path(__file__).with_name('observation.py')]},
        'embedding_lifecycle': 'one retained real provider injected into production Hybrid; no unload during measurement',
        'performance_llm': 'M3 SmokeLLM fake; Graph disabled only for paired latency isolation',
        'end_to_end_llm': 'configured API provider; endpoint/credentials/prompt/answer not persisted',
        'fault_injection': 'mocked M6 only; real timeout/busy/recovery reuse M3 evidence'}
    if recover_llm_once:
        report.update(owner_recovery_attempt=1, original_failure_evidence={
            'file': 'real-read-only-20260920-01.json',
            'sha256': sha(output.parent / 'real-read-only-20260920-01.json')})
    # Explicitly allow only these host paths requested for environment diagnosis.
    # Arbitrary report values still pass through the existing strict sanitizer.
    execution_paths = {'python_executable': sys.executable, 'cwd': str(Path.cwd())}
    report['environment'] = {
        'proxy_env_names_present': sorted(k for k in os.environ if k.lower() in
            {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}),
        'process_env_names_present': sorted(k for k in os.environ if k.startswith(('LLM_', 'PHASE12_', 'HF_', 'TRANSFORMERS_'))),
        'settings_lifecycle': 'fresh process; get_settings cached base; frozen copy changes reranker only; no config reload needed',
        'provider_lifecycle': 'clear singleton before build; explicit provider from frozen settings; same instance for preflight and RAG; close on exit',
        'settings_cache_clear_count': 0, 'sdk_max_retries': 0,
        'execution_permission': 'network-enabled execution requested for Owner-authorized recovery'}
    env_paths = [p for p in (ROOT / '.env', ROOT / 'backend/.env') if p.exists()]
    env_before = {p: sha(p) for p in env_paths}
    service = provider = sampler = repo = llm = client = None
    observation = None
    def save():
        sanitized = sanitize_report(report)
        sanitized['environment'].update(execution_paths)
        output.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    save()
    stage = 'preflight'
    try:
        base = get_settings()
        assert not base.reranker_enabled, 'production must remain disabled'
        cfg = frozen_settings(base)
        report['active_llm_metadata'] = llm_metadata(base, cfg)
        clear_llm_provider_cache()
        report['environment']['provider_cache_clear_count'] = 1
        assert sha(FIXTURES / 'golden_manifest.json') == GOLDEN_SHA, 'Golden bytes drift'
        report['golden_file_sha256'] = GOLDEN_SHA  # File integrity only; never JSON-parse Golden.
        verify_local_model(cfg)
        corpus = json.loads((FIXTURES / 'corpus_manifest.json').read_text(encoding='utf-8'))
        assert corpus['corpus_fingerprint'] == CORPUS_FINGERPRINT
        _, report['live_audit_before'] = verify_live_corpus(corpus, cfg)
        deletion_target = load_phase10_integration_settings(os.environ)
        report['real_destructive_deletion'] = 'AUTHORIZATION_BLOCKED' if deletion_target is None else 'DEDICATED_TARGET_REQUIRES_SEPARATE_PHASE10_RUN'
        import torch
        assert torch.cuda.is_available() and torch.cuda.is_bf16_supported()
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        sampler = DeviceSampler(torch)
        sampler.thread.start()
        stage = 'cold_load'
        embedding = get_embedding_provider(cfg)
        client = get_search_engine_client(cfg)
        # Retain Embedding during cold load and every forward; production config unchanged.
        embedding.encode_query(QUERIES[0][1])
        observation = Observation(torch, runtime._load_local_model)
        provider = runtime.LocalCrossEncoderProvider(RerankerConfig.from_settings(cfg), model_loader=observation.load)
        cold_started = perf_counter()
        provider._ensure_loaded()  # Explicit test preloading, cold time kept separate from warm SLO.
        report['cold_load_ms'] = (perf_counter() - cold_started) * 1000
        assert observation.loads[0]['parameter_dtype'] == 'torch.bfloat16'
        service = reranking.RerankingService(cfg, provider_factory=lambda _: provider)
        repo = Neo4jRepository(cfg)
        graph = GraphRetrievalService(cfg, repository=repo)
        llm = build_llm_provider(cfg)
        report['environment'].update(provider_class=type(llm).__name__,
            transport_class=type(llm._transport).__name__)
        real_hybrid = hybrid_search.hybrid_search_chunks
        fetch = repo.fetch_table_context
        graph_rows = []
        def observed_fetch(request):
            result = fetch(request)
            graph_rows.append({'graph_id': request.graph_id,
                'anchors': [r.anchor_id for r in request.anchors],
                'statuses': [r.status for r in result]})
            return result
        def live_hybrid(db, **kwargs):
            return real_hybrid(db, **kwargs, client=client, embedding_provider=embedding,
                               deletion_filter_session_factory=read_only_session)
        def request(kind, query, k, *, graph_enabled, real_llm, enabled=True):
            active = cfg.model_copy(update={'reranker_enabled': enabled, 'graph_retrieval_enabled': graph_enabled})
            assert_frozen_settings(active)
            before, graph_before = len(observation.forwards), len(graph_rows)
            trace = RequestTrace(service, graph)
            row = {'case': kind, 'k': k, 'graph_enabled': graph_enabled, 'real_llm': real_llm,
                'query_sha256': hashlib.sha256(query.encode('utf-8')).hexdigest(), 'status': 'RUNNING'}
            report['requests'].append(row)
            try:
                with trace:
                    answer = rag.answer_question(None, query, limit=k, settings=active,
                        llm_provider=llm if real_llm else SmokeLLM(), graph_retrieval=graph)
                trace.verify(answer)
                assert trace.filter_observed
                assert trace.hybrid_limits == [32 if enabled and k <= 32 else k]
                assert answer.context_status == 'ok'
                if enabled and k <= 32:
                    assert trace.applied and trace.fallback_reason is None, 'real reranker failure'
                    assert len(observation.forwards) - before == (len(trace.reranker_ids) + 7) // 8
                else:
                    assert len(observation.forwards) == before
                    assert trace.fallback_reason == ('disabled' if not enabled else 'public_limit_exceeds_reranker_capacity')
                if not graph_enabled:
                    assert not trace.graph_calls and len(graph_rows) == graph_before
                public = RagAskData.from_service_result(answer).model_dump(mode='json')
                assert set(public) == {'question', 'answer', 'context_status', 'citations', 'retrieval', 'llm', 'graph'}
                row.update(status='PASS', citation_ids=[c.chunk_id for c in answer.citations],
                    graph_status=answer.graph_context.status if answer.graph_context else None,
                    answer_nonempty=bool(answer.answer.strip()))
            finally:
                row.update(trace.record(), bge_forward_count=len(observation.forwards) - before,
                    repository_calls=graph_rows[graph_before:], gpu=gpu_snapshot(torch))
                save()
            sampler.check()
            return trace
        with patch.object(hybrid_search, 'hybrid_search_chunks', side_effect=live_hybrid), \
             patch.object(reranking, 'get_reranking_service', return_value=service), \
             patch.object(repo, 'fetch_table_context', side_effect=observed_fetch):
            stage = 'real_end_to_end'
            if recover_llm_once:
                stage = 'controlled_llm_recovery'
                def original_rag_case():
                    assert llm._transport._client.max_retries == 0
                    request(QUERIES[0][0], QUERIES[0][1], 8, graph_enabled=True, real_llm=True)
                controlled_llm_recovery(report, lambda: llm_generate_preflight(llm, report), original_rag_case)
                save()
                stage = 'remaining_real_end_to_end'
            for kind, query in QUERIES[1:] if recover_llm_once else QUERIES:
                request(kind, query, 8, graph_enabled=True, real_llm=True)
            request('K_equals_C', QUERIES[2][1], 32, graph_enabled=True, real_llm=True)
            request('K_exceeds_C', QUERIES[2][1], 50, graph_enabled=True, real_llm=True)
            request('graph_off', QUERIES[2][1], 8, graph_enabled=False, real_llm=True)
            request('dedicated_graph_on', GRAPH_QUERY, 8, graph_enabled=True, real_llm=True)
            request('dedicated_graph_off', GRAPH_QUERY, 8, graph_enabled=False, real_llm=True)
            assert any('success' in r['statuses'] for r in graph_rows), 'real Graph success unexercised'
            assert all(s not in {'unavailable', 'timeout', 'budget_exhausted'} for r in graph_rows for s in r['statuses']), 'real Graph unavailable'
            stage = 'paired_warm_latency'
            for n in range(20):
                kind, query = QUERIES[n % len(QUERIES)]
                off = request(f'latency_off_{n}', query, 8, graph_enabled=False, real_llm=False, enabled=False)
                on = request(f'latency_on_{n}', query, 8, graph_enabled=False, real_llm=False)
                report['pairs'].append({'pair': n, 'kind': kind, 'warm_rerank_ms': on.rerank_ms,
                    'retrieval_off_ms': off.hybrid_ms + off.rerank_ms,
                    'retrieval_on_ms': on.hybrid_ms + on.rerank_ms,
                    'incremental_ms': on.hybrid_ms + on.rerank_ms - off.hybrid_ms - off.rerank_ms})
                save()
            peak = max(s[1]['device_used_bytes'] for s in sampler.samples) / 1024 ** 2
            stage = 'performance_acceptance'
            report['performance'] = validate_performance(
                [r['warm_rerank_ms'] for r in report['pairs']],
                [r['incremental_ms'] for r in report['pairs']], peak)
        stage = 'integrity_after'
        _, report['live_audit_after'] = verify_live_corpus(corpus, cfg)
        assert sha(FIXTURES / 'golden_manifest.json') == GOLDEN_SHA
        verify_local_model(cfg)
        report['status'] = 'REAL_READ_ONLY_PASS'
    except Exception as exc:
        # Only stable application codes are retained; never exception text or repr.
        record_failure(report, stage, exc)
    finally:
        if sampler is not None:
            sampler.close()
            report['gpu_sample_count'] = len(sampler.samples)
            report['gpu_device_peak_mib'] = max((s[1]['device_used_bytes'] for s in sampler.samples), default=0) / 1024 ** 2
            report['gpu_max_sampling_gap_ms'] = max((1000 * (b[0] - a[0]) for a, b in zip(sampler.samples, sampler.samples[1:])), default=0)
        if service is not None:
            service.close()
            report['worker_closed'] = service.wait_closed(0)
        elif provider is not None:
            provider.close()
        for resource in (repo, llm, client):
            if resource is not None:
                resource.close()
        if observation is not None:
            report['model_loads'] = observation.loads
            report['total_bge_forward_count'] = len(observation.forwards)
        report['actual_env_unchanged'] = all(sha(p) == h for p, h in env_before.items())
        report['production_reranker_enabled'] = bool(get_settings().reranker_enabled)
        save()
    return {key: report[key] for key in ('status', 'profile_id', 'final_run_count')} | {
        'completed_requests': sum(r['status'] == 'PASS' for r in report['requests']),
        'failure_stage': report.get('failure_stage'), 'error_type': report.get('error_type')}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--recover-llm-once', action='store_true')
    args = parser.parse_args()
    result = run(args.output, recover_llm_once=args.recover_llm_once)
    print(json.dumps(result))
    raise SystemExit(0 if result['status'] == 'REAL_READ_ONLY_PASS' else 1)
