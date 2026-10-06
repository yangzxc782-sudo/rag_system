import { isUuid, type TurnInput } from "./qa-sessions";

// Best-effort metadata only. Browser storage is never a message history store.
const PREFIX = "qa.pending.v1:";
const CREATE = "qa.create.v1";
function read(key: string) { try { return sessionStorage.getItem(key); } catch { return null; } }
function write(key: string, value: string | null) {
  try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value); } catch { /* private mode / quota */ }
}
export function readPending(threadId: string): TurnInput | null {
  try {
    const value = JSON.parse(read(PREFIX + threadId) ?? "null");
    if (!value || value.thread_id !== threadId || !isUuid(value.request_id) || typeof value.question !== "string"
      || !value.question.trim() || value.question.length > 2000
      || !(value.limit === null || (Number.isInteger(value.limit) && value.limit >= 1 && value.limit <= 50))
      || !(value.document_id === null || (typeof value.document_id === "string" && isUuid(value.document_id)))
      || !(value.casting_input_file_id == null || (typeof value.casting_input_file_id === "string" && isUuid(value.casting_input_file_id)))) return null;
    return { request_id: value.request_id, question: value.question, limit: value.limit, document_id: value.document_id,
      ...(value.casting_input_file_id != null ? { casting_input_file_id: value.casting_input_file_id } : {}) };
  } catch { return null; }
}
export function savePending(threadId: string, input: TurnInput) {
  write(PREFIX + threadId, JSON.stringify({ thread_id: threadId, request_id: input.request_id, question: input.question,
    limit: input.limit, document_id: input.document_id,
    ...(input.casting_input_file_id != null ? { casting_input_file_id: input.casting_input_file_id } : {}) }));
}
export function clearPending(threadId: string, requestId: string) {
  if (readPending(threadId)?.request_id === requestId) write(PREFIX + threadId, null);
}
export function creationRequestId() {
  const saved = read(CREATE);
  const id = saved && isUuid(saved) ? saved : crypto.randomUUID();
  write(CREATE, id);
  return id;
}
export function clearCreation(requestId: string) { if (read(CREATE) === requestId) write(CREATE, null); }
