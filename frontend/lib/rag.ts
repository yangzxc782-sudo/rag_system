import { API_BASE_URL } from "@/lib/api";
import type { ApiEnvelope, ApiError, HybridSearchData } from "@/lib/search";

export type RagAskRequest = {
  question: string;
  limit?: number;
  document_id?: string | null;
};

export type RagCitationItem = {
  citation_id: number;
  chunk_id: string;
  document_id: string;
  original_filename?: string | null;
  chunk_index?: number | null;
  content: string;
  hybrid_score?: number | null;
  retrieval_source?: string | null;
};

export type RagLlmInfo = {
  provider?: string | null;
  model?: string | null;
};

export type RagGraphEntity = {
  id: string;
  name: string;
  entity_type: string;
  page: number | null;
};

export type RagGraphRelationship = {
  source_entity_id: string;
  source_name: string;
  type: string;
  target_entity_id: string;
  target_name: string;
};

export type RagGraphEvidence = {
  graph_id: string;
  anchor_id: string;
  anchor_type: string;
  table_ref: string | null;
  source_citations: number[];
  document: { doc_id: string };
  table: { table_id: string; table_ref: string; page: number | null; table_index: number | null };
  entities: RagGraphEntity[];
  relationships: RagGraphRelationship[];
};

export type RagGraphData = {
  enabled: boolean;
  triggered: boolean;
  status: "success" | "partial" | "not_triggered" | "unavailable";
  truncated: boolean;
  evidence_count: number;
  evidence: RagGraphEvidence[];
};

export type RagAskData = {
  question: string;
  answer: string;
  context_status: "ok" | "no_context";
  citations: RagCitationItem[];
  retrieval: HybridSearchData;
  llm: RagLlmInfo;
  graph?: RagGraphData | null;
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

export function friendlyRagErrorMessage(error: ApiError | null): string {
  switch (error?.code) {
    case "RAG_QUERY_EMPTY":
      return "请输入问题。";
    case "LLM_UNAVAILABLE":
      return "大语言模型服务不可用，请检查当前 Provider 配置与服务状态。";
    case "LLM_TIMEOUT":
      return "大语言模型响应超时，请稍后重试。";
    case "LLM_GENERATION_FAILED":
      return "大语言模型生成失败，请稍后重试。";
    case "SEARCH_ENGINE_UNAVAILABLE":
      return "搜索服务不可用，请检查 OpenSearch。";
    case "SEARCH_INDEX_NOT_FOUND":
      return "搜索索引不存在，请先创建并同步索引。";
    case "HYBRID_SEARCH_FAILED":
      return "知识检索失败，请检查搜索索引配置或稍后重试。";
    case "RAG_CONFIG_INVALID":
      return "RAG 配置异常，请检查后端配置。";
    case "RAG_ANSWER_FAILED":
      return "问答链路执行失败，请稍后重试。";
    case "FRONTEND_REQUEST_FAILED":
      return "网络请求失败，请确认后端服务可访问。";
    default:
      return error?.message ?? "知识问答请求失败，请稍后重试。";
  }
}

export async function ragAsk(request: RagAskRequest): Promise<ApiEnvelope<RagAskData>> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/rag/ask`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        question: request.question,
        limit: request.limit ?? 8,
        document_id: request.document_id || undefined,
      }),
    });

    return parseEnvelope<RagAskData>(response);
  } catch (error) {
    return fallbackError<RagAskData>("知识问答请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}
