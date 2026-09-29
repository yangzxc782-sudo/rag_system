"""M5 additive status contract: reload recovery uses saved inputs, not defaults."""
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from app.schemas.conversations import RequestStatusResponse, TurnCreateRequest
from app.services.conversations import Conversations


def test_status_includes_exact_saved_request_input():
    sid, rid, did = uuid4(), uuid4(), uuid4()
    turn = SimpleNamespace(id=uuid4(), status="needs_recovery", outcome=None,
        error_code="QA_EXECUTION_INTERRUPTED", question=" 冒口尺寸如何确定？ ",
        retrieval_limit=3, document_id=did, session_id=sid, turn_no=1)
    repo = Mock()
    repo.get_turn_by_request.return_value = turn
    repo.get_user_message.return_value = SimpleNamespace(id=uuid4())
    repo.has_later_turn.return_value = False
    service = object.__new__(Conversations)
    service.settings = SimpleNamespace(api_v1_prefix="/api/v1", rag_top_k=8)
    result = service._status(repo, sid, rid)
    assert result.can_retry and result.input == TurnCreateRequest(
        request_id=rid, question=turn.question, limit=3, document_id=did)
    assert set(result.input.model_dump()) == {"request_id", "question", "limit", "document_id"}


def test_status_serialization_does_not_require_client_execution_state():
    response = RequestStatusResponse(thread_id=uuid4(), turn_id=uuid4(), request_id=uuid4(),
        status="failed", can_retry=False, execution_active=False, status_url="/request",
        user_message_id=uuid4())
    assert response.input is None  # Additive response field; no request schema change.
