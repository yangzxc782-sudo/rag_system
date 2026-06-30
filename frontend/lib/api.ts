export type ServiceStatusName = "ok" | "warning" | "failed" | "disconnected";

export type BasicHealthResult = {
  status: ServiceStatusName;
  detail: string;
  raw?: unknown;
};

export type ServiceCheck = {
  status: ServiceStatusName;
  detail: string;
  bucket?: string;
};

export type ServiceHealthResult = {
  status: ServiceStatusName;
  services: Record<string, ServiceCheck>;
  detail?: string;
  raw?: unknown;
};

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

async function getJson(path: string): Promise<unknown> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    cache: "no-store",
    headers: {
      Accept: "application/json",
    },
  });

  if (!response.ok) {
    return {
      status: "failed",
      detail: `请求失败：HTTP ${response.status}`,
    };
  }

  return response.json();
}

function normalizeStatus(value: unknown): ServiceStatusName {
  if (value === "ok" || value === "warning" || value === "failed") {
    return value;
  }

  return "disconnected";
}

function toBasicHealthResult(value: unknown, fallbackDetail: string): BasicHealthResult {
  if (typeof value === "object" && value !== null && "status" in value) {
    const data = value as { status?: unknown; detail?: unknown; service?: unknown };
    return {
      status: normalizeStatus(data.status),
      detail:
        typeof data.detail === "string"
          ? data.detail
          : typeof data.service === "string"
            ? `${data.service} 已响应`
            : fallbackDetail,
      raw: value,
    };
  }

  return {
    status: "disconnected",
    detail: fallbackDetail,
    raw: value,
  };
}

function toServiceCheck(value: unknown): ServiceCheck {
  if (typeof value === "object" && value !== null) {
    const data = value as { status?: unknown; detail?: unknown; bucket?: unknown };
    return {
      status: normalizeStatus(data.status),
      detail: typeof data.detail === "string" ? data.detail : "未返回详细信息",
      bucket: typeof data.bucket === "string" ? data.bucket : undefined,
    };
  }

  return {
    status: "disconnected",
    detail: "未返回服务状态",
  };
}

function normalizeServiceHealth(value: unknown): ServiceHealthResult {
  if (typeof value !== "object" || value === null || !("services" in value)) {
    return {
      status: "disconnected",
      detail: "后端服务状态接口未连接",
      services: {},
      raw: value,
    };
  }

  const data = value as {
    status?: unknown;
    services?: Record<string, unknown>;
    detail?: unknown;
  };

  const services = Object.fromEntries(
    Object.entries(data.services ?? {}).map(([name, service]) => [name, toServiceCheck(service)]),
  );

  return {
    status: normalizeStatus(data.status),
    detail: typeof data.detail === "string" ? data.detail : undefined,
    services,
    raw: value,
  };
}

async function safeRequest<T>(request: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await request();
  } catch {
    return fallback;
  }
}

export async function getHealth(): Promise<BasicHealthResult> {
  return safeRequest(
    async () => toBasicHealthResult(await getJson("/health"), "FastAPI 应用已响应"),
    {
      status: "disconnected",
      detail: "FastAPI 应用未连接",
    },
  );
}

export async function getApiHealth(): Promise<BasicHealthResult> {
  return safeRequest(
    async () => toBasicHealthResult(await getJson("/api/v1/health"), "API v1 已响应"),
    {
      status: "disconnected",
      detail: "API v1 未连接",
    },
  );
}

export async function getServiceHealth(): Promise<ServiceHealthResult> {
  return safeRequest(
    async () => normalizeServiceHealth(await getJson("/api/v1/health/services")),
    {
      status: "disconnected",
      detail: "服务状态接口未连接",
      services: {},
    },
  );
}
