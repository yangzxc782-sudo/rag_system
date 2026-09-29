import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.conversation_rag import STAGE_ADAPTER, GenerationDetails, ResultDetails
from app.rag.conversation_nodes import stage_fingerprint
from app.rag.conversation_state import ExecutionIdentity


@pytest.mark.parametrize("field", ["prompt", "evidence", "graph", "answer", "history"])
def test_stage_metadata_rejects_uncontrolled_text_fields(field):
    data = GenerationDetails(outcome="answer", snapshot_keys=["answer"]).model_dump(mode="json")
    with pytest.raises(ValidationError):
        STAGE_ADAPTER.validate_json(json.dumps({**data, field: "uncontrolled text"}))


def test_stage_round_trip_has_small_reference_only_result():
    data = ResultDetails(outcome="answer", generation_artifact_id=uuid4(), evidence_artifact_id=uuid4())
    assert STAGE_ADAPTER.validate_json(data.model_dump_json()) == data
    assert len(data.model_dump_json().encode()) < 300
    with pytest.raises(ValidationError):
        ResultDetails(outcome="answer", generation_artifact_id=uuid4(), snapshot_payload={})


def test_stage_fingerprint_separates_thread_turn_attempt_parent_and_stage():
    from dataclasses import replace
    identity = ExecutionIdentity(uuid4(), uuid4(), uuid4(), 1, "a" * 64)
    parent = uuid4()
    values = {stage_fingerprint(identity, "evidence", parent),
              stage_fingerprint(replace(identity, thread_id=uuid4()), "evidence", parent),
              stage_fingerprint(replace(identity, request_id=uuid4()), "evidence", parent),
              stage_fingerprint(replace(identity, attempt_no=2), "evidence", parent),
              stage_fingerprint(replace(identity, turn_id=uuid4()), "evidence", parent),
              stage_fingerprint(identity, "evidence", uuid4()),
              stage_fingerprint(identity, "generation", parent)}
    assert len(values) == 7
