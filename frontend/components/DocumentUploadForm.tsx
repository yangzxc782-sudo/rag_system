"use client";

import { type FormEvent, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { uploadDocument } from "@/lib/documents";

const ACCEPTED_FILE_TYPES = ".pdf";

export default function DocumentUploadForm() {
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setMessage(null);
    setError(null);

    const file = fileInputRef.current?.files?.[0];
    if (!file) {
      setError("请选择一个文件。");
      return;
    }
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setError("仅支持 PDF 文档。");
      return;
    }

    setIsUploading(true);
    const result = await uploadDocument(file);
    setIsUploading(false);

    if (!result.success) {
      setError(result.error?.message ?? "上传失败。");
      return;
    }

    form.reset();
    setMessage("上传成功。");
    router.refresh();
  }

  return (
    <aside className="rounded-md border border-slate-200 bg-white shadow-sm">
      <div className="border-b border-slate-200 px-5 py-4">
        <h2 className="text-lg font-semibold text-slate-950">上传文档</h2>
        <p className="mt-1 text-sm leading-6 text-slate-600">
          仅支持 PDF 文档，解析后保存清洗正文和来源信息。
        </p>
      </div>

      <form className="flex flex-col gap-4 p-5" onSubmit={handleSubmit}>
        <label className="flex flex-col gap-2 text-sm font-medium text-slate-900">
          选择文件
          <input
            ref={fileInputRef}
            name="file"
            type="file"
            accept={ACCEPTED_FILE_TYPES}
            disabled={isUploading}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-slate-900 file:px-3 file:py-2 file:text-sm file:font-medium file:text-white disabled:cursor-not-allowed disabled:bg-slate-100"
          />
        </label>

        <button
          type="submit"
          disabled={isUploading}
          className="rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-400"
        >
          {isUploading ? "上传中..." : "上传"}
        </button>

        {message ? (
          <p className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
            {message}
          </p>
        ) : null}

        {error ? (
          <p className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">{error}</p>
        ) : null}
      </form>
    </aside>
  );
}
