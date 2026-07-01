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
