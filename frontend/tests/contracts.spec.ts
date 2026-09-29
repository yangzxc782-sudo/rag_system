import { expect, test } from "@playwright/test";
import { mergeMessages, mergeSessions, qaErrorMessage, retryDelay } from "../lib/qa-sessions";
import { A, B, MockConversations } from "./mock-conversations";

test("stable message identity and sequence merge", () => {
  const api = new MockConversations();
  api.complete(A, { request_id: B, question: "冒口？", limit: 8, document_id: null });
  const [user, assistant] = api.messages.get(A)!;
  expect(mergeMessages([assistant, user], [{ ...assistant, content: "refreshed" }])).toEqual([user, { ...assistant, content: "refreshed" }]);
});
test("session deduplication and stable cursor order", () => {
  const api = new MockConversations();
  expect(mergeSessions(api.sessions, [api.session(A, "改名")]).map(s => [s.thread_id, s.title])).toEqual([[B, "会话 B"], [A, "改名"]]);
});
test("bounded Retry-After supports seconds and HTTP dates", () => {
  expect(retryDelay("0")).toBe(1000); expect(retryDelay("1000")).toBe(30000);
  expect(retryDelay("garbage")).toBeNull(); expect(retryDelay(null)).toBeNull();
  expect(retryDelay(new Date(Date.now() + 60000).toUTCString())).toBe(30000);
});
for (const code of ["IDEMPOTENCY_CONFLICT", "THREAD_BUSY", "QA_TURN_SUPERSEDED", "QA_RECOVERY_CONFLICT",
  "QA_SESSION_NOT_FOUND", "QA_REQUEST_NOT_FOUND", "QA_SERVICE_UNAVAILABLE", "QA_EXECUTION_LOCK_LOST",
  "QA_EVIDENCE_UNAVAILABLE", "QA_FEATURE_DISABLED", "QA_LOCAL_TRANSPORT_REQUIRED", "QA_LOCAL_ACCESS_ONLY",
  "LLM_TIMEOUT", "LLM_RATE_LIMITED", "LLM_UNAVAILABLE", "LLM_RESPONSE_INVALID",
  "QA_REWRITE_OUTPUT_INVALID", "QA_REWRITE_INPUT_BUDGET_EXCEEDED", "QA_CONTEXT_BUDGET_EXCEEDED",
  "QA_REWRITE_STRATEGY_UNSUPPORTED"]) {
  test(`Chinese safe error mapping: ${code}`, () => {
    expect(qaErrorMessage(code)).not.toContain(code); expect(qaErrorMessage(code)).not.toBe(qaErrorMessage("unknown"));
  });
}
