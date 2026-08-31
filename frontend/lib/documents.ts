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
  deletion_status: DocumentDeletionState;
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

export type DocumentDeletionState = "normal" | "deleting" | "delete_failed";

export type DocumentDeletionPublicStatus = "normal" | "deleting" | "retrying" | "delete_failed";

export type DocumentDeletionStatusData = {
  document_id: string;
  status: DocumentDeletionPublicStatus;
  step_attempts: number;
  next_retry_at: string | null;
  last_error_code: string | null;
  updated_at: string;
};

export type DocumentDeletionApiResult = ApiEnvelope<DocumentDeletionStatusData> & {
  httpStatus: number | null;
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

export type ParseOutputStatus = "saved" | "download_deferred" | "unavailable";

export type DocumentParseRunRead = {
  id: string;
  document_id: string;
  parser_provider: string;
  parser_version: string | null;
  parse_mode: string | null;
  status: string;
  is_active: boolean;
  input_file_key: string | null;
  output_prefix: string | null;
  output_markdown_key: string | null;
  output_json_key: string | null;
  output_markdown_status: ParseOutputStatus;
  output_json_status: ParseOutputStatus;
  failure_status_persisted: boolean | null;
  page_count: number | null;
  block_count: number | null;
  asset_count: number | null;
  error_message: string | null;
  source_metadata_summary: Record<string, unknown>;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
};

export type DocumentParseRunListData = {
  items: DocumentParseRunRead[];
  total: number;
  limit: number;
  offset: number;
};

export type DocumentParseStatusRead = {
  document_id: string;
  process_status: string;
  latest_parse_run: DocumentParseRunRead | null;
  active_parse_run: DocumentParseRunRead | null;
};

export type DocumentBlockRead = {
  id: string;
  document_id: string;
  parse_run_id: string;
  block_index: number;
  block_key: string | null;
  block_type: string;
  page_start: number | null;
  page_end: number | null;
  bbox: unknown;
  text: string | null;
  markdown: string | null;
  html: string | null;
  latex: string | null;
  caption: string | null;
  parent_block_key: string | null;
  section_path: string[];
  confidence: number | null;
  source_metadata_summary: Record<string, unknown>;
  content_truncated: boolean;
  created_at: string;
};

export type PaginatedDocumentBlocks = {
  items: DocumentBlockRead[];
  total: number;
  limit: number;
  offset: number;
};

export type DocumentAssetRead = {
  id: string;
  document_id: string;
  parse_run_id: string;
  asset_type: string;
  page_number: number | null;
  asset_key: string;
  filename: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  caption: string | null;
  source_block_key: string | null;
  source_metadata_summary: Record<string, unknown>;
  created_at: string;
};

export type PaginatedDocumentAssets = {
  items: DocumentAssetRead[];
  total: number;
  limit: number;
  offset: number;
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

async function parseDocumentDeletionResponse(response: Response): Promise<DocumentDeletionApiResult> {
  if (response.status === 204) {
    return {
      success: true,
      data: null,
      error: null,
      httpStatus: response.status,
    };
  }

  return {
    ...(await parseEnvelope<DocumentDeletionStatusData>(response)),
    httpStatus: response.status,
  };
}

function documentDeletionRequestFailure(message: string, error: unknown): DocumentDeletionApiResult {
  return {
    ...fallbackError<DocumentDeletionStatusData>(message, {
      error_type: error instanceof Error ? error.name : typeof error,
    }),
    httpStatus: null,
  };
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

export async function deleteDocument(id: string): Promise<DocumentDeletionApiResult> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/documents/${id}`, {
      method: "DELETE",
      headers: {
        Accept: "application/json",
      },
    });

    return parseDocumentDeletionResponse(response);
  } catch (error) {
    return documentDeletionRequestFailure("文档删除请求失败。", error);
  }
}

export async function getDocumentDeletionStatus(id: string): Promise<DocumentDeletionApiResult> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/documents/${id}/deletion-status`, {
      cache: "no-store",
      headers: {
        Accept: "application/json",
      },
    });

    return parseDocumentDeletionResponse(response);
  } catch (error) {
    return documentDeletionRequestFailure("文档删除状态请求失败。", error);
  }
}

export async function retryDocumentDeletion(id: string): Promise<DocumentDeletionApiResult> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/documents/${id}/deletion/retry`, {
      method: "POST",
      headers: {
        Accept: "application/json",
      },
    });

    return parseDocumentDeletionResponse(response);
  } catch (error) {
    return documentDeletionRequestFailure("文档删除重试请求失败。", error);
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

export async function getDocumentParseRuns(
  id: string,
  params: { limit?: number; offset?: number } = {},
): Promise<ApiEnvelope<DocumentParseRunListData>> {
  const searchParams = new URLSearchParams({
    limit: String(params.limit ?? 50),
    offset: String(params.offset ?? 0),
  });

  try {
    const response = await fetch(
      `${API_BASE_URL}/api/v1/documents/${id}/parse-runs?${searchParams}`,
      {
        cache: "no-store",
        headers: { Accept: "application/json" },
      },
    );
    return parseEnvelope<DocumentParseRunListData>(response);
  } catch (error) {
    return fallbackError<DocumentParseRunListData>("解析任务列表请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}

export async function getDocumentParseStatus(
  id: string,
): Promise<ApiEnvelope<DocumentParseStatusRead>> {
  try {
    const response = await fetch(
      `${API_BASE_URL}/api/v1/documents/${id}/parse-status`,
      {
        cache: "no-store",
        headers: { Accept: "application/json" },
      },
    );
    return parseEnvelope<DocumentParseStatusRead>(response);
  } catch (error) {
    return fallbackError<DocumentParseStatusRead>("解析状态请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}

export async function getDocumentBlocks(
  id: string,
  params: {
    parseRunId?: string;
    blockType?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<ApiEnvelope<PaginatedDocumentBlocks>> {
  const searchParams = new URLSearchParams({
    limit: String(params.limit ?? 50),
    offset: String(params.offset ?? 0),
  });
  if (params.parseRunId) {
    searchParams.set("parse_run_id", params.parseRunId);
  }
  if (params.blockType) {
    searchParams.set("block_type", params.blockType);
  }

  try {
    const response = await fetch(
      `${API_BASE_URL}/api/v1/documents/${id}/blocks?${searchParams}`,
      {
        cache: "no-store",
        headers: { Accept: "application/json" },
      },
    );
    return parseEnvelope<PaginatedDocumentBlocks>(response);
  } catch (error) {
    return fallbackError<PaginatedDocumentBlocks>("结构块列表请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}

export async function getDocumentAssets(
  id: string,
  params: {
    parseRunId?: string;
    assetType?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<ApiEnvelope<PaginatedDocumentAssets>> {
  const searchParams = new URLSearchParams({
    limit: String(params.limit ?? 50),
    offset: String(params.offset ?? 0),
  });
  if (params.parseRunId) {
    searchParams.set("parse_run_id", params.parseRunId);
  }
  if (params.assetType) {
    searchParams.set("asset_type", params.assetType);
  }

  try {
    const response = await fetch(
      `${API_BASE_URL}/api/v1/documents/${id}/assets?${searchParams}`,
      {
        cache: "no-store",
        headers: { Accept: "application/json" },
      },
    );
    return parseEnvelope<PaginatedDocumentAssets>(response);
  } catch (error) {
    return fallbackError<PaginatedDocumentAssets>("解析资产列表请求失败。", {
      error_type: error instanceof Error ? error.name : typeof error,
    });
  }
}
