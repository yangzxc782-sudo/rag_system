from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.conversations import SessionCreateRequest, SessionUpdateRequest, TurnCreateRequest
from app.services.conversation_history import decode_cursor, encode_cursor
from app.services.conversation_repository import ConversationError


@pytest.mark.parametrize("extra", ["history", "checkpoint_id", "attempt_no", "thread_id"])
def test_execution_fields_cannot_be_supplied(extra):
    with pytest.raises(ValidationError):
        TurnCreateRequest(request_id=uuid4(), question="冒口？", **{extra: "untrusted"})


@pytest.mark.parametrize("changes", [{"question": " "}, {"question": "中" * 2001}, {"limit": 0},
                                    {"limit": 51}, {"limit": True}, {"limit": "8"}])
def test_invalid_turn(changes):
    with pytest.raises(ValidationError):
        TurnCreateRequest.model_validate({"request_id": uuid4(), "question": "冒口？", **changes})


def test_create_and_title_and_exact_question():
    assert SessionCreateRequest(request_id=str(uuid4())).request_id
    assert SessionUpdateRequest(title=" 工艺 ").title == "工艺"
    assert TurnCreateRequest(request_id=uuid4(), question=" 冒口？ ").question == " 冒口？ "
    with pytest.raises(ValidationError):
        SessionUpdateRequest(title=" ")


def test_cursor_roundtrip():
    row = SimpleNamespace(id=uuid4(), updated_at=datetime.now(timezone.utc))
    assert decode_cursor(encode_cursor(row)) == (row.updated_at, row.id)


@pytest.mark.parametrize("value", ["?", "x" * 257, "e30=", "WzEsMl0=", "W10="])
def test_invalid_cursor(value):
    with pytest.raises(ConversationError) as error:
        decode_cursor(value)
    assert error.value.status_code == 422
