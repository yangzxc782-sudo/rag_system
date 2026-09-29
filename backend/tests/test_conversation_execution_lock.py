from uuid import UUID, uuid4

import pytest

from app.db.conversation_lock import thread_lock_key
from app.rag.conversation_state import ExecutionIdentity
from app.services.conversation_recovery import same_execution


def test_lock_key_is_stable_signed_and_uuid_scoped():
    key = thread_lock_key(UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
    assert key == thread_lock_key(UUID("AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"))
    assert -(2 ** 63) <= key < 2 ** 63
    assert len({thread_lock_key(uuid4()) for _ in range(1000)}) == 1000


@pytest.mark.parametrize("field", ["thread_id", "turn_id", "request_id", "attempt_no", "input_fingerprint"])
def test_checkpoint_identity_requires_every_component(field):
    from types import SimpleNamespace
    row = SimpleNamespace(session_id=uuid4(), id=uuid4(), request_id=uuid4(), attempt_no=2, request_fingerprint="a" * 64)
    values = {"thread_id": str(row.session_id), "turn_id": str(row.id), "request_id": str(row.request_id),
              "attempt_no": row.attempt_no, "input_fingerprint": row.request_fingerprint}
    assert same_execution(values, row)
    values[field] = 3 if field == "attempt_no" else ("b" * 64 if field == "input_fingerprint" else str(uuid4()))
    assert not same_execution(values, row)
