import { API_BASE_URL } from "./api";
import type { RagCitationItem, RagGraphData, RagLlmInfo } from "./rag-evidence";
import type { CastingAnswerInfo } from "./casting-design";

export type TurnInput = { request_id: string; question: string; limit: number | null; document_id: string | null; casting_input_file_id?: string | null };
export type TurnStatus = "running" | "finalizing" | "completed" | "failed" | "needs_recovery";
export type Outcome = "answer" | "no_context" | "clarification" | "casting_design";
export type QaSession = { thread_id: string; title: string; created_at: string; updated_at: string };
export type SessionPage = { items: QaSession[]; next_cursor: string | null };
export type EvidenceSource = {
  snapshot_id: string; kind: "citation" | "graph"; citation_id: number | null;
  document_ids: string[]; chunk_ids: string[];
  status: "available" | "source_deleted" | "source_unavailable";
};
export type ConversationAnswer = {
  question: string; answer: string; context_status: "ok" | "no_context" | "clarification" | "casting_design";
  citations: RagCitationItem[]; graph: RagGraphData | null; llm: RagLlmInfo; sources: EvidenceSource[];
  casting?: CastingAnswerInfo | null;
};
export type RequestStatus = {
  thread_id: string; turn_id: string; request_id: string; status: TurnStatus;
  outcome: Outcome | null; error_code: string | null; can_retry: boolean; execution_active: boolean;
  status_url: string; user_message_id: string; assistant_message_id: string | null;
  result: ConversationAnswer | null; input?: TurnInput | null;
};
export type SessionDetail = QaSession & { active_request: RequestStatus | null };
export type QaMessage = {
  message_id: string; sequence_no: number; role: string; content: string; created_at: string;
  turn_id: string | null; request_id: string | null; status: TurnStatus | null;
  outcome: Outcome | null; result: ConversationAnswer | null;
  casting_input_file_id?: string | null; effective_casting_input_file_id?: string | null;
  casting_input_filename?: string | null;
};
export type MessagePage = { thread_id: string; items: QaMessage[]; next_before_seq: number | null };
export type ApiResult<T> = { data: T; status: number; retryAfterMs: number | null; location: string | null };

export type FieldIssue = { field_path: string; error_code: string; message: string };
export function safeIssues(detail: unknown): FieldIssue[] {
  if (!detail || typeof detail !== "object" || !("issues" in detail) || !Array.isArray(detail.issues)) return [];
  return detail.issues.slice(0, 100).flatMap(item => item && [item.field_path, item.error_code, item.message]
    .every(v => typeof v === "string" && v.length <= 512)
    ? [{ field_path: item.field_path, error_code: item.error_code, message: item.message }] : []);
}
export class QaApiError extends Error {
  constructor(public code: string, public status = 0, public issues: FieldIssue[] = []) { super(code); this.name = "QaApiError"; }
}

const ERROR_MESSAGES: Record<string, string> = {
  IDEMPOTENCY_CONFLICT: "原请求编号已用于不同的问题或参数。请核对请求状态，不要更改参数重试。",
  THREAD_BUSY: "此会话已有请求正在处理，请先查看当前请求状态。",
  QA_TURN_SUPERSEDED: "此轮之后已有新的问题，无法再重试这一轮。",
  QA_RECOVERY_CONFLICT: "此请求无法安全恢复，请查看当前状态或新建对话。",
  QA_SESSION_NOT_FOUND: "会话不存在，请从历史列表选择其他会话。",
  QA_REQUEST_NOT_FOUND: "服务器尚未保存此请求。可以使用原请求重试。",
  QA_SERVICE_UNAVAILABLE: "对话服务暂不可用，请稍后查询状态。",
  QA_EXECUTION_LOCK_LOST: "执行连接中断，请查询状态后决定是否重试。",
  QA_EVIDENCE_UNAVAILABLE: "本轮证据来源已失效，请查询状态后重试。",
  QA_FEATURE_DISABLED: "多轮对话功能尚未启用。请由管理员完成隔离验证与数据库准备后启用。",
  QA_LOCAL_TRANSPORT_REQUIRED: "请使用本机安全入口 python -m app.local_server 启动后端。",
  QA_LOCAL_ACCESS_ONLY: "多轮对话仅限可信本机访问，请直接连接本机后端，不要使用转发代理。",
  QA_EXECUTION_INTERRUPTED: "请求执行中断，请查询状态后恢复原请求。",
  QA_PUBLICATION_INTERRUPTED: "回答保存过程被中断，请查询状态后恢复原请求。",
  LLM_TIMEOUT: "模型响应超时，请查询本轮状态。",
  LLM_RATE_LIMITED: "模型服务请求过多，请稍后重试。",
  LLM_UNAVAILABLE: "模型服务暂不可用，请稍后重试。",
  LLM_RESPONSE_INVALID: "模型服务返回了无效内容，请查询状态后使用原请求重试。",
  QA_REWRITE_OUTPUT_INVALID: "问题理解服务返回的格式或引用无效，本轮未生成回答。可使用原请求重试。",
  QA_REWRITE_INPUT_BUDGET_EXCEEDED: "问题理解输入超过预算，请缩短问题后重新提交。",
  QA_CONTEXT_BUDGET_EXCEEDED: "必要的对话历史超过上下文预算。请在新会话中简短重述对象、条件和完整问题。",
  QA_REWRITE_STRATEGY_UNSUPPORTED: "此轮使用已停用的问题理解策略，不能继续执行或重试。请作为新问题重新提交。",
  REQUEST_VALIDATION_ERROR: "输入不合法，请检查问题长度、检索条数和文档编号。",
  NETWORK_ERROR: "网络连接中断，后端可能仍在执行。请先查询状态。",
  REQUEST_TIMEOUT: "等待响应超时，后端可能仍在执行。正在核对请求状态。",
  INVALID_RESPONSE: "服务器响应格式或会话归属异常，请重新查询。",
  CASTING_FEATURE_DISABLED: "浇冒系统设计功能尚未启用，当前仍可进行知识问答。",
  CASTING_SERVICE_UNAVAILABLE: "工程计算服务暂不可用，请稍后重试。",
  CASTING_FILE_TOO_LARGE: "工程 JSON 文件不能超过 256 KiB。",
  CASTING_FILE_TYPE: "请选择一个 .json 文件。",
  CASTING_FILE_NOT_FOUND: "此会话中找不到该工程文件，请重新选择或上传。",
  CASTING_FILE_NOT_READY: "工程文件尚未上传完成，请重试上传。",
  CASTING_INPUT_INVALID: "JSON 未通过结构检查，请按字段提示修正后重新上传。",
  CASTING_REQUEST_INVALID: "工程请求信息无效，请检查所选文件。",
  CASTING_UPLOAD_CONFLICT: "上传编号已对应其他文件，请重新选择文件。",
  CASTING_ADMISSION_FAILED: "工程输入未通过准入，请修正 JSON 后重新上传。",
  CASTING_INPUT_REQUIRED: "请上传或明确选择工程输入 JSON。",
  CASTING_RULE_NOT_APPLICABLE: "当前规则不适用于此输入，请核对材料、工艺和浇注方式。",
  CASTING_RULE_AMBIGUOUS: "存在多个适用规则版本，请由管理员检查规则配置。",
  CASTING_BUSY: "计算服务繁忙，请稍后查询状态并显式重试。",
  CASTING_CAPACITY_EXCEEDED: "输入的计算规模超过上限，请缩小热节或位置候选范围。",
  CASTING_STORAGE_UNAVAILABLE: "工程文件存储暂不可用，请稍后重试。",
  CASTING_PROVENANCE_INVALID: "工程结果来源核验未通过，未发布方案，请联系管理员检查运行记录。",
  CASTING_TOOL_CALL_INVALID: "模型返回的工程工具调用无效，本轮未发布方案。",
  CASTING_OUTPUT_INVALID: "工程结果核验未通过，请检查运行记录。",
  CASTING_DOWNLOAD_FAILED: "结果下载失败，请稍后重试。",
};
export function qaErrorMessage(error: unknown): string {
  const code = error instanceof QaApiError ? error.code : typeof error === "string" ? error : "";
  return ERROR_MESSAGES[code] ?? (error instanceof QaApiError && error.status === 422
    ? ERROR_MESSAGES.REQUEST_VALIDATION_ERROR : "暂时无法完成操作，请稍后查询或重试。");
}
export function isAbort(error: unknown) { return error instanceof DOMException && error.name === "AbortError"; }
export function isUuid(value: string) { return /^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$/i.test(value); }

// Retry-After is optional across CORS. Clamp both server hints and our fallback.
export function retryDelay(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  const delay = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(value) - Date.now();
  return Number.isFinite(delay) ? Math.min(30_000, Math.max(1000, delay)) : null;
}

export async function qaRequest<T>(path: string, signal: AbortSignal, body?: object, method = "GET"): Promise<ApiResult<T>> {
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), method === "POST" ? 120_000 : 15_000);
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      method, cache: "no-store", credentials: "omit",
      signal: AbortSignal.any([signal, timeout.signal]),
      headers: body && !(body instanceof FormData) ? { "Content-Type": "application/json" } : undefined,
      body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
    });
    const envelope = await response.json().catch(() => { throw new QaApiError("INVALID_RESPONSE", response.status); });
    if (!response.ok || envelope.success !== true) {
      const code = typeof envelope.error?.code === "string" ? envelope.error.code : "INVALID_RESPONSE";
      throw new QaApiError(code, response.status, code.startsWith("CASTING_") ? safeIssues(envelope.error?.detail) : []);
    }
    if (!envelope.data || typeof envelope.data !== "object") throw new QaApiError("INVALID_RESPONSE", response.status);
    return { data: envelope.data as T, status: response.status, location: response.headers.get("Location"),
      retryAfterMs: retryDelay(response.headers.get("Retry-After")) };
  } catch (error) {
    if (signal.aborted) throw new DOMException("View closed", "AbortError");
    if (timeout.signal.aborted) throw new QaApiError("REQUEST_TIMEOUT");
    if (error instanceof QaApiError) throw error;
    throw new QaApiError("NETWORK_ERROR");
  } finally { clearTimeout(timer); }
}
const request = qaRequest;

const base = "/api/v1/rag/sessions";
function sessionPath(threadId: string) {
  if (!isUuid(threadId)) throw new QaApiError("QA_SESSION_NOT_FOUND", 404);
  return `${base}/${threadId}`;
}
function owned<T extends { thread_id: string }>(result: ApiResult<T>, threadId: string): ApiResult<T> {
  if (result.data.thread_id !== threadId) throw new QaApiError("INVALID_RESPONSE");
  return result;
}
function requestOwned(result: ApiResult<RequestStatus>, threadId: string, requestId: string) {
  owned(result, threadId);
  if (result.data.request_id !== requestId || (result.data.input && result.data.input.request_id !== requestId)) {
    throw new QaApiError("INVALID_RESPONSE");
  }
  return result;
}

export const qaSessions = {
  create: (requestId: string, signal: AbortSignal) => request<QaSession>(base, signal, { request_id: requestId }, "POST"),
  list: (signal: AbortSignal, cursor?: string | null) => request<SessionPage>(`${base}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, signal),
  detail: async (threadId: string, signal: AbortSignal) => owned(await request<SessionDetail>(sessionPath(threadId), signal), threadId),
  rename: async (threadId: string, title: string, signal: AbortSignal) => owned(await request<QaSession>(sessionPath(threadId), signal, { title }, "PATCH"), threadId),
  messages: async (threadId: string, signal: AbortSignal, before?: number | null) => owned(await request<MessagePage>(`${sessionPath(threadId)}/messages?limit=50${before ? `&before_seq=${before}` : ""}`, signal), threadId),
  submit: async (threadId: string, input: TurnInput, signal: AbortSignal) => requestOwned(await request<RequestStatus>(`${sessionPath(threadId)}/turns`, signal, input, "POST"), threadId, input.request_id),
  status: async (threadId: string, requestId: string, signal: AbortSignal, statusUrl?: string | null) => {
    if (!isUuid(requestId)) throw new QaApiError("INVALID_RESPONSE");
    const path = `${sessionPath(threadId)}/requests/${requestId}`;
    // Accept the server's URL only when it names this exact request on this API.
    if (statusUrl && new URL(statusUrl, API_BASE_URL).href !== new URL(path, API_BASE_URL).href) {
      throw new QaApiError("INVALID_RESPONSE");
    }
    return requestOwned(await request<RequestStatus>(path, signal), threadId, requestId);
  },
};

export function mergeMessages(older: QaMessage[], newer: QaMessage[]): QaMessage[] {
  return [...new Map([...older, ...newer].map(m => [m.message_id, m])).values()]
    .sort((a, b) => a.sequence_no - b.sequence_no || a.message_id.localeCompare(b.message_id));
}
export function mergeSessions(older: QaSession[], newer: QaSession[]): QaSession[] {
  return [...new Map([...older, ...newer].map(s => [s.thread_id, s])).values()]
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at) || b.thread_id.localeCompare(a.thread_id));
}
