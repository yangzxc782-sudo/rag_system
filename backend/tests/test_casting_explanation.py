"""Frozen full-source explanations, without model/engine/storage side effects."""
import json
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from app.casting.execution_protocol import digest
from app.rag.casting_prompt import EXPLANATION_PROMPT, MAX_EXPLANATION_CONTEXT_BYTES, explanation_request
from app.schemas.casting_storage import CastingStorageError
from app.services.casting_design import CastingDesignService


FIXTURES = Path(__file__).parent / "fixtures/casting"
ASSETS = Path(__file__).parents[1] / "app/casting/vendor/v5_1"


def full_sources(fixture="baseline"):
    return {"input": json.loads((ASSETS / "input-v1.json").read_bytes()),
            "rules": json.loads((ASSETS / "rules-v1.json").read_bytes()),
            "recommendation": json.loads((FIXTURES / f"{fixture}.recommendation.json").read_bytes())}


@pytest.mark.parametrize("fixture", ["baseline", "no_candidate"])
def test_full_sources_fit_context_without_projection_or_truncation(fixture):
    sources = full_sources(fixture)
    request = explanation_request("RS-01 高度为什么是 220 mm？C01 和 C02 有什么区别？", sources, candidate_rank=2)
    supplied = json.loads(request.messages[-1].content[0].text)
    assert {key: supplied[key] for key in sources} == sources
    assert supplied["candidate_rank"] == 2 and "220 mm" in supplied["question"]
    assert request.tools == () and not request.json_mode and request.max_tokens >= 2048
    assert sum(len(m.content[0].text.encode()) for m in request.messages) <= MAX_EXPLANATION_CONTEXT_BYTES
    assert request.messages[0].content[0].text == EXPLANATION_PROMPT


def test_budget_includes_question_and_unicode_does_not_send_partial_sources():
    with pytest.raises(CastingStorageError) as exc:
        explanation_request("追问" * MAX_EXPLANATION_CONTEXT_BYTES, full_sources())
    assert exc.value.code == "CASTING_EXPLANATION_TOO_LARGE"


class FrozenService(CastingDesignService):
    def __init__(self):
        self.sid, self.rid = uuid4(), uuid4()
        self.objects = {k: json.dumps(v).encode() for k, v in full_sources().items()}
        self.reads = []
        self.files = NS(artifact_bytes=self.artifact)
        self.row = NS(status="succeeded", input_sha256=digest(self.objects["input"]),
                      rule_sha256=digest(self.objects["rules"]), result_sha256=digest(self.objects["recommendation"]))
        # No engine attribute, current assets, rule selection, or input-file reader.

    def _row(self, sid, rid):
        assert (sid, rid) == (self.sid, self.rid)
        return self.row

    def artifact(self, sid, rid, name):
        assert (sid, rid) == (self.sid, self.rid)
        self.reads.append(name)
        if name.removesuffix(".json") not in self.objects:
            raise CastingStorageError("CASTING_SNAPSHOT_NOT_READY", "missing snapshot", status=409)
        return self.objects[name.removesuffix(".json")]

    def recommendation(self, sid, rid):
        assert (sid, rid) == (self.sid, self.rid)
        return self.objects["recommendation"], self.row.result_sha256


def test_sources_read_exact_run_objects_without_accessing_engine():
    service = FrozenService()
    assert service.explanation_sources(service.sid, service.rid) == full_sources()
    assert service.reads == ["input.json", "rules.json"]


@pytest.mark.parametrize("key", ["input", "rules", "recommendation"])
def test_sources_reject_hash_mismatch(key):
    service = FrozenService()
    service.objects[key] += b" "
    with pytest.raises(CastingStorageError) as exc:
        service.explanation_sources(service.sid, service.rid)
    assert exc.value.safe_detail.category == "integrity"


@pytest.mark.parametrize("name", ["input", "rules"])
def test_missing_snapshot_never_falls_back_to_current_files(name):
    service = FrozenService()
    del service.objects[name]
    with pytest.raises(CastingStorageError) as exc:
        service.explanation_sources(service.sid, service.rid)
    assert exc.value.code == "CASTING_SNAPSHOT_NOT_READY"


def test_nonterminal_run_is_not_explainable():
    service = FrozenService()
    service.row.status = "running"
    with pytest.raises(CastingStorageError) as exc:
        service.explanation_sources(service.sid, service.rid)
    assert exc.value.code == "CASTING_RESULT_NOT_READY" and not service.reads
