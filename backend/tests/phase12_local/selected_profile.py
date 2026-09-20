"""Owner-frozen M5 identity reused by M6/M7; no model loads or dataset access."""
import hashlib
import json
from pathlib import Path

from tests.phase12_local.corpus_audit import fingerprint


PROFILE_ID = 'bf16-L1024-C32-B8'
PROFILE_FINGERPRINT = '3c7efd44de1b2c7fece6b142ec58cc41d88f4dadf5160aef9cf439fcd565b5c7'
PROFILE_PATH = Path(__file__).resolve().parents[1] / 'fixtures/phase12/selected_profile.json'


def validate_selected_profile(data):
    if (data.get('profile_id') != PROFILE_ID
            or data.get('profile_fingerprint') != PROFILE_FINGERPRINT
            or fingerprint(data.get('identity')) != PROFILE_FINGERPRINT):
        raise ValueError('selected profile fingerprint drift')
    if (data.get('status') != 'PHASE12_M5_ACCEPTED'
            or data.get('selection_policy') != 'owner quality-first decision'
            or data.get('known_paraphrase_risk') != 'accepted known Phase 12 v1 risk'
            or data.get('production_enabled') is not False or data.get('final_run_count') != 0):
        raise ValueError('selected profile owner contract drift')
    return data


def read_selected_profile():
    return validate_selected_profile(json.loads(PROFILE_PATH.read_text(encoding='utf-8')))


def frozen_settings(base):
    identity = read_selected_profile()['identity']
    # Only the test scope is enabled. Keep storage/LLM/Graph settings and local
    # weight location from the existing configuration; never mutate actual .env.
    fields = ('provider', 'model', 'dtype', 'device', 'max_length',
              'candidate_limit', 'batch_size', 'timeout_seconds')
    result = base.model_copy(update={
        **{'reranker_' + key: identity[key] for key in fields}, 'reranker_enabled': True,
    })
    assert_frozen_settings(result)
    return result


def assert_frozen_settings(settings):
    identity = read_selected_profile()['identity']
    fields = ('provider', 'model', 'dtype', 'device', 'max_length',
              'candidate_limit', 'batch_size', 'timeout_seconds')
    if any(getattr(settings, 'reranker_' + key) != identity[key] for key in fields):
        raise ValueError('selected profile settings drift')
    from app.retrieval.reranker import RerankerConfig
    RerankerConfig.from_settings(settings)


def verify_local_model(settings):
    assert_frozen_settings(settings)
    for entry in read_selected_profile()['identity']['model_files']:
        path = Path(settings.reranker_model_path) / entry['name']
        if path.stat().st_size != entry['size_bytes']:
            raise ValueError('local model identity drift')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != entry['sha256']:
                raise ValueError('local model identity drift')
    return PROFILE_FINGERPRINT
