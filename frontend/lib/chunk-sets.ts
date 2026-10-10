import { API_BASE_URL } from "@/lib/api";
import type { ApiEnvelope } from "@/lib/documents";

export const FROZEN_STATUSES = ["cleaned_source_ready", "kg_extracting", "kg_writing", "kg_failed", "kg_ready", "kg_ready_empty",
  "chunking", "chunks_ready", "embedding", "indexing", "retrieval_indexed", "retrieval_failed"];

export type BlockChunkerConfig = {
  max_chunk_chars: number; min_chunk_chars: number; overlap_chars: number; max_table_chars: number;
  keep_table_intact: boolean; keep_formula_with_context: boolean;
};
export const SEGMENTATION_VERSION = "pdf-block-aware-codepoints-v1";
export type SegmentationDefaults = { segmentation_defaults: BlockChunkerConfig; segmentation_version: string };
const configKeys = ["max_chunk_chars", "min_chunk_chars", "overlap_chars", "max_table_chars",
  "keep_table_intact", "keep_formula_with_context"] as const;

export function configError(value: unknown): string | null {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).length !== configKeys.length || !configKeys.every(k => k in value)) {
    return "切分配置必须包含完整六字段；旧配置格式不再支持。";
  }
  const c = value as BlockChunkerConfig;
  if (![c.max_chunk_chars, c.min_chunk_chars, c.overlap_chars, c.max_table_chars].every(Number.isSafeInteger)
    || typeof c.keep_table_intact !== "boolean" || typeof c.keep_formula_with_context !== "boolean") {
    return "长度必须是整数，结构保护开关必须是布尔值。";
  }
  if (c.max_chunk_chars < 1 || c.max_chunk_chars > 60000 || c.max_table_chars < 1) return "正文目标须为 1–60000，表格分组阈值须大于 0。";
  if (c.min_chunk_chars < 0 || c.min_chunk_chars > c.max_chunk_chars) return "小尾块阈值须在 0 与正文目标之间；缩小正文目标时请同时调整小尾块阈值。";
  if (c.overlap_chars < 0 || c.overlap_chars >= c.max_chunk_chars) return "期望重叠长度须非负且小于正文目标。";
  return null;
}

export function readSegmentationDefaults(value: SegmentationDefaults): BlockChunkerConfig {
  if (value.segmentation_version !== SEGMENTATION_VERSION || configError(value.segmentation_defaults)) {
    throw new Error("后端切分默认配置缺失或版本不受支持，请刷新后再提交。");
  }
  return { ...value.segmentation_defaults };
}
export type ChunkSetStatus = {
  chunk_set_id: string; source_version: string; graph_build_id: string; request_id: string;
  status: string; job_status: string; stage: string; chunk_count: number | null;
  embedding_counts: Record<string, number>; segmentation_config: BlockChunkerConfig;
  segmentation_version: string; segmentation_config_sha256: string;
  is_current: boolean; publication_revision: number; last_error_code: string | null;
  can_advance: boolean; search_enabled: boolean;
  managed: boolean; job_id: string | null;
};
export type ChunkSets = SegmentationDefaults & {
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
