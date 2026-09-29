"""Explicit retry policy. Requires the caller's thread execution lease."""
from app.core.errors import LLM_ERROR_STATUS_CODES
from app.rag.conversation_state import ExecutionIdentity
from app.rag.query_rewrite_prompt import current_rewrite_policy
from app.services.conversation_repository import ConversationError


INTERRUPTED = {"QA_EXECUTION_INTERRUPTED", "QA_PUBLICATION_INTERRUPTED"}
SOURCE_ERRORS = {"QA_EVIDENCE_UNAVAILABLE", "QA_SOURCE_INVALID", "DOCUMENT_NOT_FOUND",
                 "DOCUMENT_DELETION_IN_PROGRESS", "DOCUMENT_DELETE_FAILED"}
RETRYABLE = INTERRUPTED | SOURCE_ERRORS | {
    "EMBEDDING_GENERATION_FAILED", "SEARCH_ENGINE_UNAVAILABLE", "SEARCH_INDEX_NOT_FOUND",
    "HYBRID_SEARCH_FAILED", "RAG_ANSWER_FAILED", "QA_STAGE_FAILED", "QA_PROMPT_CHANGED", "QA_REWRITE_OUTPUT_INVALID",
    *LLM_ERROR_STATUS_CODES,
}
FINAL_FAILURES = RETRYABLE | {"QA_PROMPT_BUDGET_EXCEEDED", "QA_CONTEXT_BUDGET_EXCEEDED",
                            "QA_REWRITE_INPUT_BUDGET_EXCEEDED", "QA_REWRITE_STRATEGY_UNSUPPORTED"}
REWRITE_ERROR_STATUS = {"QA_REWRITE_OUTPUT_INVALID": 502, "QA_REWRITE_INPUT_BUDGET_EXCEEDED": 422,
                       "QA_CONTEXT_BUDGET_EXCEEDED": 422, "QA_REWRITE_STRATEGY_UNSUPPORTED": 409}


def policy_supported(repo, turn):
    try:
        repo.require_rewrite_policy(turn, current_rewrite_policy())
        return True
    except ConversationError as exc:
        if exc.code != "QA_REWRITE_STRATEGY_UNSUPPORTED":
            raise
        return False


def reject_legacy(repo, turn):
    """Called under the thread lease; never rewrites or publishes old work."""
    if turn is not None and turn.status != "completed" and not policy_supported(repo, turn):
        if turn.status in {"running", "finalizing", "needs_recovery"}:
            repo.transition_turn(turn.session_id, turn.id, expected_status=turn.status,
                new_status="failed", expected_attempt=turn.attempt_no, error_code="QA_REWRITE_STRATEGY_UNSUPPORTED")
        return True
    return False


def can_retry(repo, turn, *, active=False):
    return (not active and turn.status in {"failed", "needs_recovery", "running", "finalizing"}
            and not repo.has_later_turn(turn.session_id, turn.turn_no)
            and (turn.error_code is None or turn.error_code in RETRYABLE)
            and policy_supported(repo, turn))


def same_execution(values, turn):
    return bool(values) and ExecutionIdentity.from_state(values) == ExecutionIdentity(
        turn.session_id, turn.id, turn.request_id, turn.attempt_no, turn.request_fingerprint)


def prepare_retry(repo, turn, checkpoint):
    """Same-attempt replay for interrupted commits; new attempt for ended failures.

    A graph needs_recovery terminal is never bypassed by invoke. This explicit
    retry either advances its business attempt or refuses unsupported corruption.
    """
    if repo.has_later_turn(turn.session_id, turn.turn_no):
        raise ConversationError("QA_TURN_SUPERSEDED", "A later turn prevents retrying this request.", status_code=409)
    repo.require_rewrite_policy(turn, current_rewrite_policy())
    values = checkpoint.values
    terminal_failure = same_execution(values, turn) and values.get("terminal_status") == "needs_recovery"
    code = values.get("error_code") if terminal_failure else turn.error_code
    if terminal_failure or turn.status == "failed" or (turn.status == "needs_recovery" and code not in INTERRUPTED):
        if code not in RETRYABLE:
            raise ConversationError("QA_RECOVERY_CONFLICT", "This failure requires operator review.", status_code=409)
        if turn.status in {"running", "finalizing"}:
            turn = repo.transition_turn(turn.session_id, turn.id, expected_status=turn.status,
                new_status="failed", expected_attempt=turn.attempt_no, error_code=code)
        turn = repo.transition_turn(turn.session_id, turn.id, expected_status=turn.status,
            new_status="running", expected_attempt=turn.attempt_no, rewrite_policy=current_rewrite_policy())
        return turn
    if turn.status in {"finalizing", "needs_recovery"}:
        return repo.restore_interrupted_turn(turn.session_id, turn.id, expected_attempt=turn.attempt_no)
    return turn


def mark_interrupted(repo, turn, *, publication=False):
    if turn.status in {"running", "finalizing"}:
        return repo.transition_turn(turn.session_id, turn.id, expected_status=turn.status,
            new_status="needs_recovery", expected_attempt=turn.attempt_no,
            error_code="QA_PUBLICATION_INTERRUPTED" if publication else "QA_EXECUTION_INTERRUPTED")
    return turn


def record_failure(repo, turn, code):
    """Handled failures release the thread; integrity failures require review."""
    if turn.status in {"running", "finalizing"}:
        return repo.transition_turn(turn.session_id, turn.id, expected_status=turn.status,
            new_status="failed" if code in FINAL_FAILURES else "needs_recovery",
            expected_attempt=turn.attempt_no, error_code=code)
    return turn


def confirm_terminal(graph, turn):
    staged = graph.get_result(turn.session_id, turn.id, turn.request_id, turn.attempt_no)
    checkpoint = graph.get_state(turn.session_id)
    values = checkpoint.values
    if (not staged.checkpoint_complete or checkpoint.next or not same_execution(values, turn)
            or values.get("terminal_status") != "result_staged" or values.get("error_code") is not None
            or values.get("result_artifact_id") != str(staged.result_artifact_id)
            or values.get("outcome") != staged.outcome
            or staged.thread_id != turn.session_id or staged.turn_id != turn.id or staged.attempt_no != turn.attempt_no):
        raise ConversationError("QA_CHECKPOINT_RESULT_MISMATCH", "Terminal checkpoint and result do not match.", status_code=409)
    return staged
