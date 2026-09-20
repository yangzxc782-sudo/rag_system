"""Recovery control flow is tested without any real provider calls."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.core.errors import BusinessError, LLM_UNAVAILABLE
from tests.phase12_local.selected_profile import frozen_settings


def test_preflight_failure_never_calls_rag_or_retries():
    from tests.phase12_integration.real_acceptance import controlled_llm_recovery
    preflight = Mock(side_effect=BusinessError(LLM_UNAVAILABLE, 'private'))
    rag_case = Mock()
    report = {}
    with pytest.raises(BusinessError):
        controlled_llm_recovery(report, preflight, rag_case)
    preflight.assert_called_once()
    rag_case.assert_not_called()
    assert report['recovery_status'] == 'M6_REAL_RUNNER_LLM_CONNECTION_REPRODUCIBLE'


def test_rag_failure_never_retries_or_marks_transient():
    from tests.phase12_integration.real_acceptance import controlled_llm_recovery
    preflight = Mock(return_value={'status': 'PASS'})
    rag_case = Mock(side_effect=BusinessError(LLM_UNAVAILABLE, 'private'))
    report = {}
    with pytest.raises(BusinessError):
        controlled_llm_recovery(report, preflight, rag_case)
    preflight.assert_called_once()
    rag_case.assert_called_once()
    assert report['recovery_status'] == 'M6_REAL_RUNNER_LLM_CONNECTION_REPRODUCIBLE'
    assert report['rag_retry_status'] == 'FAIL'


def test_only_success_marks_original_failure_recovered():
    from tests.phase12_integration.real_acceptance import controlled_llm_recovery
    calls = []
    report = {}
    controlled_llm_recovery(report, lambda: calls.append('preflight'), lambda: calls.append('rag'))
    assert calls == ['preflight', 'rag']
    assert report['rag_retry_status'] == 'PASS'
    assert report['recovery_status'] == 'TRANSIENT_REAL_LLM_CONNECTION_FAILURE_RECOVERED'


def test_metadata_checks_frozen_copy_and_never_serializes_key():
    from tests.phase12_integration.real_acceptance import llm_metadata
    base = Settings(_env_file=None, llm_provider='api', llm_remote_model='gpt-4o-mini',
                    llm_remote_base_url='https://llm.example.test/v1', llm_remote_api_key='private-unit-key',
                    llm_remote_timeout_seconds=60)
    actual = frozen_settings(base)
    metadata = llm_metadata(base, actual)
    assert metadata['provider'] == 'api' and metadata['model'] == 'gpt-4o-mini'
    assert metadata['base_url_host'] == 'llm.example.test' and metadata['base_url_path'] == '/v1'
    assert metadata['remote_api_key'] == 'SET' and metadata['timeout_seconds'] == 60
    assert 'private-unit-key' not in str(metadata)
    for field, value in [('llm_remote_model', 'other'), ('llm_remote_timeout_seconds', 120),
                         ('llm_remote_base_url', 'https://other.example.test/v1')]:
        with pytest.raises(AssertionError):
            llm_metadata(base, actual.model_copy(update={field: value}))


def test_project_preflight_uses_one_generate_no_timeout_or_provider_override():
    from tests.phase12_integration.real_acceptance import llm_generate_preflight
    provider = Mock()
    provider.generate.return_value = SimpleNamespace(text='PROJECT_API_OK', provider='api', model='gpt-4o-mini')
    report = {}
    llm_generate_preflight(provider, report)
    provider.generate.assert_called_once()
    request = provider.generate.call_args.args[0]
    assert request.timeout_seconds is None
    assert report['llm_preflight']['status'] == 'PASS'
    assert 'PROJECT_API_OK' not in str(report)  # Response text is not persisted.


def test_preflight_records_only_normalized_error_and_latency():
    from tests.phase12_integration.real_acceptance import llm_generate_preflight
    provider = Mock()
    provider.generate.side_effect = BusinessError(LLM_UNAVAILABLE, 'private remote body',
        detail={'error_type': 'APIConnectionError', 'body': 'private remote body'})
    report = {}
    with pytest.raises(BusinessError):
        llm_generate_preflight(provider, report)
    provider.generate.assert_called_once()
    assert report['llm_preflight']['upstream_exception_class'] == 'APIConnectionError'
    assert report['llm_preflight']['latency_ms'] >= 0
    assert 'private remote body' not in str(report)
