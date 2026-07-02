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

export type DocumentSummary = {
  id: string;
  original_filename: string;
  file_type: string | null;
  mime_type: string | null;
  file_size: number | null;
  file_hash: string | null;
  process_status: string;
  created_at: string;
  updated_at: string;
};

export type DocumentDetail = DocumentSummary & {
  bucket_name: string;
  object_key: string;
  error_message: string | null;
};

export type DocumentListData = {
  items: DocumentSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type DocumentParseData = {
  document_id: string;
  process_status: string;
  chunk_count: number;
  parser_name: string;
  parser_version: string;
};

export type DocumentEmbeddingData = {
  document_id: string;
  total: number;
  embedded: number;
  skipped: number;
  failed: number;
  model: string | null;
  dim: number | null;
  device: string | null;
};

export type DocumentEmbeddingStatusData = {
  document_id: string;
  total: number;
  not_started: number;
  embedding: number;
  embedded: number;
  embed_failed: number;
  models: string[];
  dims: number[];
};

export type DocumentChunkRead = {
  id: string;
  document_id: string;
  chunk_index: number;
  content: string;
  character_count: number;
  token_count: number | null;
  page_start: number | null;
  page_end: number | null;
  section_title: string | null;
  chunk_type: string | null;
  source_metadata: Record<string, unknown> | null;
  embedding_status: string;
  created_at: string;
  updated_at: string;
};

export type DocumentChunkStats = {
  chunk_count: number;
  total_characters: number;
  min_characters: number;
  max_characters: number;
  avg_characters: number;
};

export type DocumentChunkListData = {
  items: DocumentChunkRead[];
  total: number;
  limit: number;
  offset: number;
  stats: DocumentChunkStats;
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

export async function getDocuments(
  params: { limit?: number; offset?: number } = {},
): Promise<ApiEnvelope<DocumentListData>> {
  const limit = params.limit ?? 20;
  const offset = params.offset ?? 0;
  const searchParams = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });

  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/documents?${searchParams}`, {
      cache: "no-store",
      headers: {
        Accept: "application/json",
      },
    });

    return parseEnvelope<DocumentListData>(response);
  } catch (error) {
    return fallbackError<DocumentListData>("文档列表请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}

export async function getDocument(id: string): Promise<ApiEnvelope<DocumentDetail>> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/documents/${id}`, {
      cache: "no-store",
      headers: {
        Accept: "application/json",
      },
    });

    return parseEnvelope<DocumentDetail>(response);
  } catch (error) {
    return fallbackError<DocumentDetail>("文档详情请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}

export async function uploadDocument(file: File): Promise<ApiEnvelope<DocumentDetail>> {
  const formData = new FormData();
  formData.append("file", file);

  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/documents`, {
      method: "POST",
      body: formData,
      headers: {
        Accept: "application/json",
      },
    });

    return parseEnvelope<DocumentDetail>(response);
  } catch (error) {
    return fallbackError<DocumentDetail>("文档上传请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}

export async function parseDocument(id: string): Promise<ApiEnvelope<DocumentParseData>> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/documents/${id}/parse`, {
      method: "POST",
      headers: {
        Accept: "application/json",
      },
    });

    return parseEnvelope<DocumentParseData>(response);
  } catch (error) {
    return fallbackError<DocumentParseData>("文档解析请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}

export async function generateDocumentEmbeddings(id: string): Promise<ApiEnvelope<DocumentEmbeddingData>> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/documents/${id}/embeddings`, {
      method: "POST",
      headers: {
        Accept: "application/json",
      },
    });

    return parseEnvelope<DocumentEmbeddingData>(response);
  } catch (error) {
    return fallbackError<DocumentEmbeddingData>("文档 embedding 生成请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}

export async function getDocumentEmbeddingStatus(id: string): Promise<ApiEnvelope<DocumentEmbeddingStatusData>> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/documents/${id}/embedding-status`, {
      cache: "no-store",
      headers: {
        Accept: "application/json",
      },
    });

    return parseEnvelope<DocumentEmbeddingStatusData>(response);
  } catch (error) {
    return fallbackError<DocumentEmbeddingStatusData>("文档 embedding 状态请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}

export async function getDocumentChunks(
  id: string,
  params: { limit?: number; offset?: number } = {},
): Promise<ApiEnvelope<DocumentChunkListData>> {
  const limit = params.limit ?? 50;
  const offset = params.offset ?? 0;
  const searchParams = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });

  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/documents/${id}/chunks?${searchParams}`, {
      cache: "no-store",
      headers: {
        Accept: "application/json",
      },
    });

    return parseEnvelope<DocumentChunkListData>(response);
  } catch (error) {
    return fallbackError<DocumentChunkListData>("文档切片列表请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}
