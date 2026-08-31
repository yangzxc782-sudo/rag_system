import type { DocumentDetail } from "@/lib/documents";

type DocumentDetailProps = {
  document: DocumentDetail;
};

function formatFileSize(fileSize: number | null): string {
  if (fileSize === null) {
    return "未知";
  }

  if (fileSize < 1024) {
    return `${fileSize} B`;
  }

  if (fileSize < 1024 * 1024) {
    return `${(fileSize / 1024).toFixed(1)} KB`;
  }

  return `${(fileSize / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(value: string): string {
  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-1 border-b border-slate-100 px-5 py-4 sm:grid-cols-[180px_1fr] sm:gap-5">
      <dt className="text-sm font-medium text-slate-500">{label}</dt>
      <dd className="break-words text-sm leading-6 text-slate-950">{value}</dd>
    </div>
  );
}

export default function DocumentDetailView({ document }: DocumentDetailProps) {
  return (
    <div>
      <dl>
        <DetailRow label="原始文件名" value={document.original_filename} />
        <DetailRow label="MinIO bucket" value={document.bucket_name} />
        <DetailRow label="Object key（本地调试）" value={document.object_key} />
        <DetailRow label="文件类型" value={document.file_type ?? "未知"} />
        <DetailRow label="MIME 类型" value={document.mime_type ?? "未知"} />
        <DetailRow label="文件大小" value={formatFileSize(document.file_size)} />
        <DetailRow label="SHA-256 哈希" value={document.file_hash ?? "未记录"} />
        <DetailRow label="处理状态" value={document.process_status} />
        <DetailRow label="删除状态" value={document.deletion_status} />
        <DetailRow label="上传时间" value={formatDate(document.created_at)} />
        <DetailRow label="更新时间" value={formatDate(document.updated_at)} />
        <DetailRow label="错误信息" value={document.error_message ?? "无"} />
      </dl>
    </div>
  );
}
