import { API_BASE_URL } from "@/lib/api";
import type { ApiEnvelope } from "@/lib/documents";
import type { SegmentationConfig } from "@/lib/chunk-sets";

export type ProcessingJob = {
  job_id: string; document_id: string; request_id: string; operation: "process" | "rechunk";
  status: string; stage: string; source_version: string | null; graph_build_id: string | null;
  chunk_set_id: string | null; parse_run_id: string | null; attempt_count: number; max_attempts: number;
  last_error_code: string | null; lease_expires_at: string | null; can_retry: boolean; can_cancel: boolean;
  cancel_requested: boolean; requires_io_reconciliation: boolean; managed: boolean;
  config: SegmentationConfig | null; created_at: string; updated_at: string;
  graph_status: string | null; unit_count: number | null; completed_units: number;
  chunk_count: number | null; embedded_count: number; chunk_set_status: string | null;
};
export type ProcessingJobs = {
  items: ProcessingJob[]; total: number; executor_enabled: boolean; search_enabled: boolean; can_process: boolean;
};

export async function processingRequest<T>(documentId: string, path = "/processing-jobs", body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}/api/v1/documents/${encodeURIComponent(documentId)}${path}`, {
    method: body === undefined ? "GET" : "POST", cache: "no-store", headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json() as ApiEnvelope<T>;
  if (!response.ok || !result.success || !result.data) {
    throw new Error(`${result.error?.message ?? "处理请求未完成，请先刷新状态"} (${result.error?.code ?? response.status})`);
  }
  return result.data;
}
