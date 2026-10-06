import { randomUUID } from "node:crypto";
import type { Page, Route } from "@playwright/test";
import type { ConversationAnswer, QaMessage, QaSession, RequestStatus, TurnInput } from "../lib/qa-sessions";

export const A = "00000000-0000-4000-8000-00000000000a";
export const B = "00000000-0000-4000-8000-00000000000b";
const timestamp = "2026-09-28T08:00:00Z";
export const ok = (route: Route, data: unknown, status = 200, headers = {}) => route.fulfill({ status,
  json: { success: true, data, error: null }, headers: { "Access-Control-Allow-Origin": "*", ...headers } });
export const fail = (route: Route, code: string, status = 503) => route.fulfill({ status,
  json: { success: false, data: null, error: { code, message: "internal detail is not UI text" } }, headers: { "Access-Control-Allow-Origin": "*" } });
export function answer(question = "冒口有什么作用？", context: ConversationAnswer["context_status"] = "ok"): ConversationAnswer {
  return { question, answer: context === "clarification" ? "你指的是冒口还是冷铁？" : context === "no_context" ? "没有找到足够的知识库依据。" : `回答：${question} [1]`,
    context_status: context, citations: context === "ok" ? [{ citation_id: 1, document_id: A, chunk_id: B, content: "冒口用于补缩。", original_filename: "工艺手册.pdf" }] : [],
    graph: null, llm: { provider: "mock", model: "test-model" }, sources: [] };
}
export class MockConversations {
  sessions: QaSession[] = [this.session(A, "会话 A"), this.session(B, "会话 B")];
  messages = new Map<string, QaMessage[]>();
  states = new Map<string, RequestStatus>();
  creates: string[] = [];
  posts: { sid: string; input: TurnInput }[] = [];
  reads: string[] = [];
  listPages = false;
  createAbort = false;
  onTurn?: (route: Route, sid: string, input: TurnInput) => Promise<void>;
  onStatus?: (route: Route, sid: string, rid: string) => Promise<void>;
  onMessages?: (route: Route, sid: string, before: string | null) => Promise<void>;
  session(threadId: string, title: string): QaSession { return { thread_id: threadId, title, created_at: timestamp, updated_at: timestamp }; }
  state(sid: string, input: TurnInput, status: RequestStatus["status"] = "running"): RequestStatus {
    return { thread_id: sid, request_id: input.request_id, turn_id: randomUUID(), status,
      outcome: null, error_code: null, can_retry: status === "needs_recovery", execution_active: status === "running",
      status_url: `/api/v1/rag/sessions/${sid}/requests/${input.request_id}`, user_message_id: randomUUID(), assistant_message_id: null, result: null, input };
  }
  begin(sid: string, input: TurnInput, status: RequestStatus["status"] = "running") {
    const key = `${sid}:${input.request_id}`;
    if (this.states.has(key)) return this.states.get(key)!;
    const state = this.state(sid, input, status), messages = this.messages.get(sid) ?? [];
    messages.push({ message_id: state.user_message_id, sequence_no: messages.length + 1, role: "user", content: input.question,
      created_at: timestamp, turn_id: state.turn_id, request_id: input.request_id, status, outcome: null, result: null,
      ...(input.casting_input_file_id ? { casting_input_file_id: input.casting_input_file_id, effective_casting_input_file_id: input.casting_input_file_id } : {}) });
    this.states.set(key, state); this.messages.set(sid, messages); return state;
  }
  complete(sid: string, input: TurnInput, result = answer(input.question)) {
    const state = this.begin(sid, input);
    if (state.status === "completed") return state;
    Object.assign(state, { status: "completed", outcome: result.context_status === "ok" ? "answer" : result.context_status,
      can_retry: false, execution_active: false, error_code: null, result, assistant_message_id: randomUUID() });
    const messages = this.messages.get(sid)!;
    const user = messages.find(m => m.message_id === state.user_message_id)!; user.status = "completed"; user.outcome = state.outcome;
    messages.push({ ...user, message_id: state.assistant_message_id!, sequence_no: messages.length + 1, role: "assistant", content: result.answer, result });
    return state;
  }
  async install(page: Page) {
    await page.route("**/api/v1/rag/**", async route => {
      const req = route.request(), url = new URL(req.url());
      if (req.method() === "OPTIONS") { await route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,PATCH,OPTIONS", "Access-Control-Allow-Headers": "content-type" } }); return; }
      const tail = url.pathname.split("/sessions")[1];
      if (tail === undefined) throw new Error("Old single-turn endpoint called");
      if (!tail && req.method() === "POST") {
        const rid = req.postDataJSON().request_id; this.creates.push(rid);
        const sid = "00000000-0000-4000-8000-00000000000c";
        let session = this.sessions.find(s => s.thread_id === sid);
        if (!session) { session = this.session(sid, "新会话"); this.sessions.unshift(session); }
        if (this.createAbort) { this.createAbort = false; await route.abort("timedout"); return; }
        await ok(route, session); return;
      }
      if (!tail) {
        await ok(route, { items: this.listPages ? (url.searchParams.has("cursor") ? this.sessions : this.sessions.slice(0, 1)) : this.sessions,
          next_cursor: this.listPages && !url.searchParams.has("cursor") ? "cursor+1=" : null }); return;
      }
      const [, sid, action, rid] = tail.split("/");
      const session = this.sessions.find(s => s.thread_id === sid);
      if (!session) { await fail(route, "QA_SESSION_NOT_FOUND", 404); return; }
      if (!action && req.method() === "PATCH") { session.title = req.postDataJSON().title; await ok(route, session); return; }
      if (!action) { await ok(route, { ...session, active_request: [...this.states.values()].find(s => s.thread_id === sid && ["running", "finalizing", "needs_recovery"].includes(s.status)) ?? null }); return; }
      if (action === "messages") {
        this.reads.push(url.pathname + url.search);
        if (this.onMessages) { await this.onMessages(route, sid, url.searchParams.get("before_seq")); return; }
        const before = Number(url.searchParams.get("before_seq") ?? Infinity);
        const all = (this.messages.get(sid) ?? []).filter(m => m.sequence_no < before), items = all.slice(-50);
        await ok(route, { thread_id: sid, items, next_before_seq: all.length > 50 ? items[0].sequence_no : null }); return;
      }
      if (action === "turns") {
        const input = req.postDataJSON() as TurnInput; this.posts.push({ sid, input });
        if (this.onTurn) { await this.onTurn(route, sid, input); return; }
        await ok(route, this.complete(sid, input)); return;
      }
      if (action === "requests") {
        this.reads.push(url.pathname);
        if (this.onStatus) { await this.onStatus(route, sid, rid); return; }
        const state = this.states.get(`${sid}:${rid}`);
        if (state) await ok(route, state); else await fail(route, "QA_REQUEST_NOT_FOUND", 404);
        return;
      }
      throw new Error(`Unexpected API request: ${req.method()} ${url}`);
    });
  }
}
