import { API_BASE_URL } from "./api";
import { isUuid, QaApiError, qaRequest, type FieldIssue } from "./qa-sessions";

export const MAX_CASTING_INPUT_BYTES = 256 * 1024;
export type CastingInputFile = {
  file_id: string; thread_id: string; request_id: string; original_filename: string;
  content_type: string; size_bytes: number; sha256: string; storage_state: "pending" | "ready";
  admission_passed: boolean; created_at: string;
};
export type CastingInputPage = { items: CastingInputFile[]; next_before_id: string | null; reusable_input?: CastingInputFile | null };
export type CastingResultStatus = "success" | "no_feasible_candidate" | "admission_failed" | "engine_failed"
  | "timed_out" | "interrupted" | "input_required" | "clarify_selection";
export type CastingAnswerInfo = {
  result_status: CastingResultStatus; route: "calculate" | "explain_existing" | "input_required" | "clarify_selection";
  run_id: string | null; result_file_id: string | null; result_sha256: string | null;
  input_file_id: string | null; input_reused: boolean; rule_id: string | null; rule_version: string | null;
  rule_sha256: string | null; recommended_candidate_id: string | null; candidate_count: number | null;
  candidate_rank: number | null; summary_mode: "llm_fact_refs" | "llm_explanation" | "template";
  error: { category: string; code: string; message: string; retryable: boolean; issues: FieldIssue[] } | null;
};
export const castingStatusLabel: Record<CastingResultStatus, string> = {
  success: "方案计算完成", no_feasible_candidate: "暂无可行方案", admission_failed: "输入未通过准入",
  engine_failed: "计算未完成", timed_out: "计算超时", interrupted: "计算中断",
  input_required: "需要工程输入", clarify_selection: "需要明确计算来源",
};
function base(threadId: string) {
  if (!isUuid(threadId)) throw new QaApiError("QA_SESSION_NOT_FOUND");
  return `/api/v1/rag/sessions/${threadId}`;
}
function owned(file: CastingInputFile, threadId: string) {
  if (!file || file.thread_id !== threadId || !isUuid(file.file_id) || !isUuid(file.request_id)
    || typeof file.original_filename !== "string" || file.original_filename.length > 255
    || !["pending", "ready"].includes(file.storage_state) || typeof file.admission_passed !== "boolean") {
    throw new QaApiError("INVALID_RESPONSE");
  }
  return file;
}
export const castingDesign = {
  async list(threadId: string, signal: AbortSignal, before?: string | null) {
    if (before && !isUuid(before)) throw new QaApiError("INVALID_RESPONSE");
    const { data } = await qaRequest<CastingInputPage>(`${base(threadId)}/casting-inputs?limit=50${before ? `&before_id=${before}` : ""}`, signal);
    if (!Array.isArray(data.items) || data.items.length > 50 || (data.next_before_id != null && !isUuid(data.next_before_id))) {
      throw new QaApiError("INVALID_RESPONSE");
    }
    data.items.forEach(file => owned(file, threadId));
    if (data.reusable_input) {
      owned(data.reusable_input, threadId);
      if (!data.reusable_input.admission_passed || data.reusable_input.storage_state !== "ready") throw new QaApiError("INVALID_RESPONSE");
    }
    return data;
  },
  async upload(threadId: string, requestId: string, file: File, signal: AbortSignal) {
    if (!isUuid(requestId)) throw new QaApiError("CASTING_REQUEST_INVALID");
    if (!file.name.toLowerCase().endsWith(".json")) throw new QaApiError("CASTING_FILE_TYPE");
    if (file.size > MAX_CASTING_INPUT_BYTES) throw new QaApiError("CASTING_FILE_TOO_LARGE");
    const body = new FormData();
    body.append("request_id", requestId); body.append("file", file);
    const { data } = await qaRequest<CastingInputFile>(`${base(threadId)}/casting-inputs`, signal, body, "POST");
    owned(data, threadId);
    if (data.request_id !== requestId || data.storage_state !== "ready") throw new QaApiError("INVALID_RESPONSE");
    return data;
  },
  async download(threadId: string, runId: string, signal: AbortSignal) {
    if (!isUuid(runId)) throw new QaApiError("CASTING_DOWNLOAD_FAILED");
    const response = await fetch(`${API_BASE_URL}${base(threadId)}/casting-runs/${runId}/recommendation`, {
      cache: "no-store", credentials: "omit", signal: AbortSignal.any([signal, AbortSignal.timeout(30_000)]),
    });
    if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw new QaApiError("CASTING_DOWNLOAD_FAILED");
    // Preserve original bytes; never parse, edit or reserialize the engineering result.
    const blob = await response.blob();
    if (signal.aborted) return;
    const url = URL.createObjectURL(blob), link = document.createElement("a");
    link.href = url; link.download = `recommendation-${runId}.json`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};
