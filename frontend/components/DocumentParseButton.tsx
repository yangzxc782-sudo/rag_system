"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { parseDocument } from "@/lib/documents";

type DocumentParseButtonProps = {
  documentId: string;
};

export default function DocumentParseButton({ documentId }: DocumentParseButtonProps) {
  const router = useRouter();
  const [isParsing, setIsParsing] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleParse() {
    setIsParsing(true);
    setMessage(null);
    setError(null);

    const result = await parseDocument(documentId);
    setIsParsing(false);

    if (!result.success) {
      if (result.error?.code === "DOCUMENT_ALREADY_PARSED") {
        setError("该文档已解析，不会覆盖已有 chunks。");
        return;
      }

      setError(result.error?.message ?? "解析失败。");
      return;
    }

    setMessage(`解析完成，生成 ${result.data?.chunk_count ?? 0} 个 chunks。`);
    router.refresh();
  }

  return (
    <div className="flex flex-col gap-3 rounded-md border border-slate-200 bg-white p-4 shadow-sm">
      <div>
        <h3 className="text-base font-semibold text-slate-950">解析与切片</h3>
        <p className="mt-1 text-sm leading-6 text-slate-600">同步触发轻量解析并生成基础 chunks。</p>
      </div>

      <button
        type="button"
        onClick={handleParse}
        disabled={isParsing}
        className="w-fit rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-400"
      >
        {isParsing ? "解析中..." : "解析文档"}
      </button>

      {message ? (
        <p className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          {message}
        </p>
      ) : null}

      {error ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">{error}</p>
      ) : null}
    </div>
  );
}
