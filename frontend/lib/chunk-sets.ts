import { API_BASE_URL } from "@/lib/api";
import type { ApiEnvelope } from "@/lib/documents";

export const FROZEN_STATUSES = ["cleaned_source_ready", "kg_extracting", "kg_writing", "kg_failed", "kg_ready", "kg_ready_empty",
  "chunking", "chunks_ready", "embedding", "indexing", "retrieval_indexed", "retrieval_failed"];

export type SegmentationConfig = { chunk_size: number; overlap: number; boundary: "characters" | "line" | "paragraph" };
export type ChunkSetStatus = {
  chunk_set_id: string; source_version: string; graph_build_id: string; request_id: string;
  status: string; job_status: string; stage: string; chunk_count: number | null;
  embedding_counts: Record<string, number>; segmentation_config: SegmentationConfig;
  is_current: boolean; publication_revision: number; last_error_code: string | null;
  can_advance: boolean; search_enabled: boolean;
  managed: boolean; job_id: string | null;
};
export type ChunkSets = {
  items: ChunkSetStatus[]; current: ChunkSetStatus | null; total: number;
  process_ready: { source_version: string; graph_build_id: string; request_id: string } | null;
};

export async function chunkSetRequest<T>(documentId: string, suffix = "", body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}/api/v1/documents/${encodeURIComponent(documentId)}/chunk-sets${suffix}`, {
    method: body === undefined ? "GET" : "POST", cache: "no-store",
    headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json() as ApiEnvelope<T>;
  if (!response.ok || !result.success || result.data === null) {
    throw new Error(`${result.error?.message ?? "切片版本请求失败"} (${result.error?.code ?? response.status})`);
  }
  return result.data;
}
