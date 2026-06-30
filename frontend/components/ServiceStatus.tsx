import type {
  BasicHealthResult,
  ServiceHealthResult,
  ServiceStatusName,
} from "@/lib/api";

type ServiceStatusProps = {
  appHealth: BasicHealthResult;
  apiHealth: BasicHealthResult;
  serviceHealth: ServiceHealthResult;
};

const serviceLabels: Record<string, string> = {
  postgresql: "PostgreSQL",
  redis: "Redis",
  minio: "MinIO",
  minio_bucket: "MinIO bucket",
};

const statusLabels: Record<ServiceStatusName, string> = {
  ok: "正常",
  warning: "警告",
  failed: "检查失败",
  disconnected: "未连接",
};

const statusStyles: Record<ServiceStatusName, string> = {
  ok: "border-emerald-200 bg-emerald-50 text-emerald-800",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  failed: "border-rose-200 bg-rose-50 text-rose-800",
  disconnected: "border-slate-200 bg-slate-50 text-slate-600",
};

function StatusBadge({ status }: { status: ServiceStatusName }) {
  return (
    <span className={`rounded-md border px-2 py-1 text-xs font-medium ${statusStyles[status]}`}>
      {statusLabels[status]}
    </span>
  );
}

export default function ServiceStatus({
  appHealth,
  apiHealth,
  serviceHealth,
}: ServiceStatusProps) {
  const services = serviceHealth.services;
  const minioBucket =
    services.minio_bucket ??
    (services.minio?.status === "warning" && services.minio.bucket
      ? {
          status: "warning" as ServiceStatusName,
          detail: services.minio.detail,
          bucket: services.minio.bucket,
        }
      : {
          status: "disconnected" as ServiceStatusName,
          detail: "等待后端返回 bucket 检查结果",
        });

  const rows: Array<{ name: string; status: ServiceStatusName; detail: string }> = [
    { name: "FastAPI 应用", status: appHealth.status, detail: appHealth.detail },
    { name: "API v1", status: apiHealth.status, detail: apiHealth.detail },
    {
      name: serviceLabels.postgresql,
      status: services.postgresql?.status ?? "disconnected",
      detail: services.postgresql?.detail ?? "未返回 PostgreSQL 状态",
    },
    {
      name: serviceLabels.redis,
      status: services.redis?.status ?? "disconnected",
      detail: services.redis?.detail ?? "未返回 Redis 状态",
    },
    {
      name: serviceLabels.minio,
      status: services.minio?.status ?? "disconnected",
      detail: services.minio?.detail ?? "未返回 MinIO 状态",
    },
    {
      name: serviceLabels.minio_bucket,
      status: minioBucket.status,
      detail: minioBucket.detail,
    },
  ];

  return (
    <section aria-label="系统状态" className="rounded-md border border-slate-200 bg-white shadow-sm">
      <div className="border-b border-slate-200 px-5 py-4">
        <h2 className="text-xl font-semibold text-slate-950">系统状态</h2>
        <p className="mt-1 text-sm text-slate-600">
          基础健康检查只展示连接状态；MinIO bucket 缺失会以警告呈现。
        </p>
      </div>

      <div className="divide-y divide-slate-100">
        {rows.map((row) => (
          <div
            key={row.name}
            className="grid gap-3 px-5 py-4 sm:grid-cols-[180px_120px_1fr] sm:items-center"
          >
            <div className="font-medium text-slate-900">{row.name}</div>
            <StatusBadge status={row.status} />
            <div className="text-sm leading-6 text-slate-600">{row.detail}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
