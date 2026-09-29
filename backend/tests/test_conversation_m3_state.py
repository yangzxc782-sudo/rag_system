from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.rag.conversation_state import StateContract, StateContractV2, validate_state
from app.rag.conversation_graph import build_conversation_graph


def identity():
    return dict(thread_id=str(uuid4()), turn_id=str(uuid4()), request_id=str(uuid4()),
                attempt_no=1, input_fingerprint="a" * 64, current_message_id=str(uuid4()))


def test_v1_remains_closed_and_readable():
    old = StateContract(**identity()).model_dump()
    assert validate_state(old) == old
    with pytest.raises(ValidationError):
        validate_state({**old, "result_artifact_id": str(uuid4())})


def test_v2_has_explicit_version_and_resets_references():
    state = StateContractV2(**identity(), evidence_generation=1).model_dump()
    assert validate_state(state) == state
    assert state["graph_version"] == "phase13_m3_v2" and state["state_schema_version"] == 2
    assert all(state[name] is None for name in ("retrieval_artifact_id", "evidence_artifact_id", "generation_artifact_id", "result_artifact_id"))
    with pytest.raises(ValidationError):
        validate_state({**state, "prompt": "not a checkpoint field"})
    with pytest.raises(ValidationError):
        validate_state({**state, "graph_version": "phase13_m2_v1"})


@pytest.mark.parametrize("outcome,path", [
    ("answer", ["retrieve_and_rerank", "build_evidence", "generate_answer", "stage_result"]),
    ("no_context", ["retrieve_and_rerank", "build_evidence", "stage_result"]),
    ("clarification", ["stage_clarification"]),
])
def test_v2_graph_compiles_and_routes_without_redundant_ready_node(outcome, path):
    from types import SimpleNamespace
    from langgraph.checkpoint.memory import InMemorySaver
    calls = []
    class RagNodes:
        def node(self, name):
            def run(state):
                calls.append(name)
                return {"outcome": outcome} if name == "build_evidence" else {}
            return run
    nodes = SimpleNamespace(load_context=lambda s: {}, understand_question=lambda s: {
        "outcome": "clarification" if outcome == "clarification" else "standalone"})
    graph = build_conversation_graph(nodes, InMemorySaver(), RagNodes())
    state = StateContractV2(**identity(), evidence_generation=1).model_dump()
    graph.invoke(state, {"configurable": {"thread_id": state["thread_id"]}})
    assert calls == path
    assert "ready_for_retrieval" not in graph.get_graph().nodes
