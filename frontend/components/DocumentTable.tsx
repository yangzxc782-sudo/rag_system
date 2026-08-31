import Link from "next/link";

import DocumentDeletionControls from "@/components/DocumentDeletionControls";
import type { DocumentSummary } from "@/lib/documents";

type DocumentTableProps = {
  documents: DocumentSummary[];
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

function documentStatusLabel(document: DocumentSummary): string {
  if (document.deletion_status === "deleting") {
    return "正在删除";
  }

  if (document.deletion_status === "delete_failed") {
    return "删除失败";
  }

  return document.process_status;
}

function documentStatusClass(document: DocumentSummary): string {
  if (document.deletion_status === "deleting") {
    return "border-amber-200 bg-amber-50 text-amber-800";
  }

  if (document.deletion_status === "delete_failed") {
    return "border-red-200 bg-red-50 text-red-800";
  }

  return "border-emerald-200 bg-emerald-50 text-emerald-800";
}

export default function DocumentTable({ documents }: DocumentTableProps) {
  if (documents.length === 0) {
    return (
      <div className="px-5 py-10 text-center text-sm text-slate-500">
        暂无文档记录。
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-slate-200 text-sm">
        <thead className="bg-slate-50 text-left text-xs font-semibold uppercase text-slate-500">
          <tr>
            <th className="px-5 py-3">文件名</th>
            <th className="px-5 py-3">类型</th>
            <th className="px-5 py-3">大小</th>
            <th className="px-5 py-3">状态</th>
            <th className="px-5 py-3">上传时间</th>
            <th className="px-5 py-3">操作</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 bg-white">
          {documents.map((document) => (
            <tr key={document.id}>
              <td className="max-w-[260px] px-5 py-4 font-medium text-slate-950">
                <span className="block truncate" title={document.original_filename}>
                  {document.original_filename}
                </span>
              </td>
              <td className="px-5 py-4 text-slate-600">{document.file_type ?? "未知"}</td>
              <td className="px-5 py-4 text-slate-600">{formatFileSize(document.file_size)}</td>
              <td className="px-5 py-4">
                <span className={`rounded-md border px-2 py-1 text-xs font-medium ${documentStatusClass(document)}`}>
                  {documentStatusLabel(document)}
                </span>
              </td>
              <td className="px-5 py-4 text-slate-600">{formatDate(document.created_at)}</td>
              <td className="px-5 py-4">
                <div className="flex flex-col items-start gap-3">
                  <Link className="font-medium text-slate-950 underline-offset-4 hover:underline" href={`/documents/${document.id}`}>
                    详情
                  </Link>
                  <DocumentDeletionControls
                    documentId={document.id}
                    initialStatus={document.deletion_status}
                  />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
