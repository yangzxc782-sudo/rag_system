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

export type VectorSearchRequest = {
  query: string;
  limit?: number;
  document_id?: string;
};

export type VectorSearchItem = {
  chunk_id: string;
  document_id: string;
  original_filename: string;
  chunk_index: number;
  content: string;
  chunk_type: string | null;
  source_metadata: Record<string, unknown> | null;
  embedding_model: string | null;
  embedding_dim: number | null;
  embedding_status: string;
  distance: number;
  score: number;
};

export type VectorSearchData = {
  query: string;
  limit: number;
  document_id: string | null;
  total: number;
  items: VectorSearchItem[];
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

export async function vectorSearch(request: VectorSearchRequest): Promise<ApiEnvelope<VectorSearchData>> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/search/vector`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        query: request.query,
        limit: request.limit ?? 10,
        document_id: request.document_id || undefined,
      }),
    });

    return parseEnvelope<VectorSearchData>(response);
  } catch (error) {
    return fallbackError<VectorSearchData>("向量检索请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}
