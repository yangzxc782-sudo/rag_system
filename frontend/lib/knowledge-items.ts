import { API_BASE_URL } from "@/lib/api";

export type ApiError = {
  code: string;
  message: string;
  detail: unknown | null;
};

export type ApiEnvelope<T> = {
  success: boolean;
  data: T | null;
  error: ApiError | null;
};

export type KnowledgeItemStatus = "draft" | "pending_review" | "approved" | "rejected" | "deprecated";

export type KnowledgeItemType =
  | "process_rule"
  | "parameter_recommendation"
  | "defect_cause"
  | "defect_solution"
  | "material_property"
  | "standard_requirement"
  | "term_definition"
  | "case_experience";

export type KnowledgeItemData = {
  id: string;
  item_type: KnowledgeItemType | string;
  title: string;
  content: string;
  content_hash: string;
  structured_data?: unknown;
  entities?: unknown;
  parameters?: unknown;
  conditions?: unknown;
  confidence?: number | null;
  status: KnowledgeItemStatus | string;
  source_document_id?: string | null;
  source_filename?: string | null;
  source_chunk_ids?: string[];
  created_by?: string | null;
  reviewed_by?: string | null;
  review_comment?: string | null;
  version: number;
  reviewed_at?: string | null;
  created_at: string;
  updated_at: string;
  revises_item_id?: string | null;
};

export type KnowledgeItemListData = {
  items: KnowledgeItemData[];
  total: number;
  limit: number;
  offset: number;
};

export type KnowledgeItemChunkData = {
  id: string;
  knowledge_item_id: string;
  chunk_id: string;
  document_id: string;
  chunk_index: number;
  source_text: string;
  created_at: string;
};

export type KnowledgeItemChunksData = {
  items: KnowledgeItemChunkData[];
  total: number;
};

export type KnowledgeItemVersionData = {
  id: string;
  knowledge_item_id: string;
  version: number;
  snapshot: unknown;
  change_reason?: string | null;
  created_by?: string | null;
  created_at: string;
};

export type KnowledgeItemVersionsData = {
  items: KnowledgeItemVersionData[];
  total: number;
};

export type KnowledgeItemReviewData = {
  id: string;
  knowledge_item_id: string;
  review_action: string;
  from_status: string;
  to_status: string;
  review_comment?: string | null;
  reviewer?: string | null;
  created_at: string;
};

export type KnowledgeItemReviewsData = {
  items: KnowledgeItemReviewData[];
  total: number;
};

export type KnowledgeExtractionRequest = {
  mode: "document" | "chunks";
  document_id?: string | null;
  chunk_ids?: string[];
  item_types?: string[];
  auto_submit?: boolean;
  max_chunks?: number;
  created_by?: string | null;
};

export type KnowledgeExtractionSkippedDuplicate = {
  item_type: string;
  title: string;
  content_hash: string;
  source_document_id?: string | null;
  reason?: string;
};

export type KnowledgeExtractionData = {
  items: KnowledgeItemData[];
  created: number;
  skipped_duplicates: KnowledgeExtractionSkippedDuplicate[];
  status: string;
  auto_submit: boolean;
  llm?: {
    provider?: string | null;
    model?: string | null;
  };
};

export type KnowledgeItemListParams = {
  status?: string;
  item_type?: string;
  source_document_id?: string;
  source_filename?: string;
  limit?: number;
  offset?: number;
};

export type KnowledgeItemCreatePayload = {
  item_type: string;
  title: string;
  content: string;
  structured_data?: unknown;
  entities?: unknown;
  parameters?: unknown;
  conditions?: unknown;
  confidence?: number | null;
  status?: "draft" | "pending_review" | null;
  source_document_id?: string | null;
  source_chunk_ids?: string[] | null;
  source_filename?: string | null;
  created_by?: string | null;
};

export type KnowledgeItemUpdatePayload = {
  title?: string;
  content?: string;
  structured_data?: unknown;
  entities?: unknown;
  parameters?: unknown;
  conditions?: unknown;
  confidence?: number | null;
  source_chunk_ids?: string[] | null;
  source_filename?: string | null;
  updated_by?: string | null;
  change_reason?: string | null;
};

export type KnowledgeItemReviewPayload = {
  reviewer?: string | null;
  review_comment?: string | null;
};

export type KnowledgeItemRevisePayload = {
  created_by?: string | null;
  change_reason?: string | null;
};

function fallbackError<T>(message: string, detail: unknown = null): ApiEnvelope<T> {
  return {
    success: false,
    data: null,
    error: {
      code: "FRONTEND_REQUEST_FAILED",
      message,
      detail,
    },
  };
}

async function parseEnvelope<T>(response: Response): Promise<ApiEnvelope<T>> {
  try {
    const body = (await response.json()) as ApiEnvelope<T>;

    if (typeof body === "object" && body !== null && "success" in body) {
      return body;
    }

    return fallbackError<T>("后端响应格式不符合预期。", body);
  } catch {
    return fallbackError<T>(`请求失败：HTTP ${response.status}`);
  }
}

function buildQuery(params: Record<string, string | number | undefined>): string {
  const searchParams = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") {
      searchParams.set(key, String(value));
    }
  });
  const query = searchParams.toString();
  return query ? `?${query}` : "";
}

async function requestJson<T>(
  path: string,
  init: RequestInit,
  fallbackMessage: string,
): Promise<ApiEnvelope<T>> {
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
      ...init,
    });
    return parseEnvelope<T>(response);
  } catch (error) {
    return fallbackError<T>(fallbackMessage, {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}

export function friendlyKnowledgeItemErrorMessage(error: ApiError | null): string {
  switch (error?.code) {
    case "KNOWLEDGE_ITEM_NOT_FOUND":
      return "知识条目不存在。";
    case "KNOWLEDGE_ITEM_INVALID_STATUS":
      return "知识条目状态无效。";
    case "KNOWLEDGE_ITEM_INVALID_TRANSITION":
      return "当前状态不允许执行该操作。";
    case "KNOWLEDGE_ITEM_VALIDATION_FAILED":
      return "知识条目字段校验失败。";
    case "KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND":
      return "来源片段不存在。";
    case "KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND":
      return "来源文档不存在。";
    case "KNOWLEDGE_ITEM_DUPLICATE":
      return "存在重复知识条目。";
    case "KNOWLEDGE_ITEM_VERSION_FAILED":
      return "版本快照写入失败。";
    case "KNOWLEDGE_ITEM_REVIEW_FAILED":
      return "审核记录写入失败。";
    case "KNOWLEDGE_ITEM_EXTRACTION_FAILED":
      return "知识条目抽取失败。";
    case "KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED":
      return "模型输出解析失败。";
    case "KNOWLEDGE_ITEM_CONFIG_INVALID":
      return "知识抽取配置或参数非法。";
    case "LLM_UNAVAILABLE":
      return "本地大语言模型服务不可用。";
    case "LLM_TIMEOUT":
      return "大语言模型响应超时。";
    case "LLM_GENERATION_FAILED":
      return "大语言模型生成失败。";
    case "FRONTEND_REQUEST_FAILED":
      return "网络请求失败，请确认后端服务可访问。";
    default:
      return error?.message ?? "知识条目请求失败，请稍后重试。";
  }
}

export function listKnowledgeItems(params: KnowledgeItemListParams): Promise<ApiEnvelope<KnowledgeItemListData>> {
  const query = buildQuery({
    status: params.status,
    item_type: params.item_type,
    source_document_id: params.source_document_id,
    source_filename: params.source_filename,
    limit: params.limit ?? 20,
    offset: params.offset ?? 0,
  });
  return requestJson<KnowledgeItemListData>(
    `/api/v1/knowledge-items${query}`,
    { method: "GET" },
    "知识条目列表请求失败。",
  );
}

export function getKnowledgeItem(id: string): Promise<ApiEnvelope<KnowledgeItemData>> {
  return requestJson<KnowledgeItemData>(
    `/api/v1/knowledge-items/${id}`,
    { method: "GET" },
    "知识条目详情请求失败。",
  );
}

export function createKnowledgeItem(payload: KnowledgeItemCreatePayload): Promise<ApiEnvelope<KnowledgeItemData>> {
  return requestJson<KnowledgeItemData>(
    "/api/v1/knowledge-items",
    { method: "POST", body: JSON.stringify(payload) },
    "知识条目创建请求失败。",
  );
}

export function updateKnowledgeItem(
  id: string,
  payload: KnowledgeItemUpdatePayload,
): Promise<ApiEnvelope<KnowledgeItemData>> {
  return requestJson<KnowledgeItemData>(
    `/api/v1/knowledge-items/${id}`,
    { method: "PATCH", body: JSON.stringify(payload) },
    "知识条目更新请求失败。",
  );
}

export function getKnowledgeItemChunks(id: string): Promise<ApiEnvelope<KnowledgeItemChunksData>> {
  return requestJson<KnowledgeItemChunksData>(
    `/api/v1/knowledge-items/${id}/chunks`,
    { method: "GET" },
    "知识条目来源片段请求失败。",
  );
}

export function getKnowledgeItemVersions(id: string): Promise<ApiEnvelope<KnowledgeItemVersionsData>> {
  return requestJson<KnowledgeItemVersionsData>(
    `/api/v1/knowledge-items/${id}/versions`,
    { method: "GET" },
    "知识条目版本请求失败。",
  );
}

export function getKnowledgeItemReviews(id: string): Promise<ApiEnvelope<KnowledgeItemReviewsData>> {
  return requestJson<KnowledgeItemReviewsData>(
    `/api/v1/knowledge-items/${id}/reviews`,
    { method: "GET" },
    "知识条目审核记录请求失败。",
  );
}

export function submitKnowledgeItem(
  id: string,
  payload: KnowledgeItemReviewPayload,
): Promise<ApiEnvelope<KnowledgeItemData>> {
  return requestJson<KnowledgeItemData>(
    `/api/v1/knowledge-items/${id}/submit`,
    { method: "POST", body: JSON.stringify(payload) },
    "知识条目提交审核请求失败。",
  );
}

export function approveKnowledgeItem(
  id: string,
  payload: KnowledgeItemReviewPayload,
): Promise<ApiEnvelope<KnowledgeItemData>> {
  return requestJson<KnowledgeItemData>(
    `/api/v1/knowledge-items/${id}/approve`,
    { method: "POST", body: JSON.stringify(payload) },
    "知识条目批准请求失败。",
  );
}

export function rejectKnowledgeItem(
  id: string,
  payload: KnowledgeItemReviewPayload,
): Promise<ApiEnvelope<KnowledgeItemData>> {
  return requestJson<KnowledgeItemData>(
    `/api/v1/knowledge-items/${id}/reject`,
    { method: "POST", body: JSON.stringify(payload) },
    "知识条目驳回请求失败。",
  );
}

export function deprecateKnowledgeItem(
  id: string,
  payload: KnowledgeItemReviewPayload,
): Promise<ApiEnvelope<KnowledgeItemData>> {
  return requestJson<KnowledgeItemData>(
    `/api/v1/knowledge-items/${id}/deprecate`,
    { method: "POST", body: JSON.stringify(payload) },
    "知识条目废弃请求失败。",
  );
}

export function reviseKnowledgeItem(
  id: string,
  payload: KnowledgeItemRevisePayload,
): Promise<ApiEnvelope<KnowledgeItemData>> {
  return requestJson<KnowledgeItemData>(
    `/api/v1/knowledge-items/${id}/revise`,
    { method: "POST", body: JSON.stringify(payload) },
    "知识条目修订请求失败。",
  );
}

export function extractKnowledgeItems(
  payload: KnowledgeExtractionRequest,
): Promise<ApiEnvelope<KnowledgeExtractionData>> {
  return requestJson<KnowledgeExtractionData>(
    "/api/v1/knowledge-items/extract",
    { method: "POST", body: JSON.stringify(payload) },
    "知识条目抽取请求失败。",
  );
}
