"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { parseDocument } from "@/lib/documents";
import { FROZEN_STATUSES } from "@/lib/chunk-sets";

type DocumentParseButtonProps = {
  documentId: string;
  disabled?: boolean;
  processStatus?: string;
  isPdf?: boolean;
};

export default function DocumentParseButton({ documentId, disabled = false, processStatus, isPdf = true }: DocumentParseButtonProps) {
  const router = useRouter();
  const [isParsing, setIsParsing] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sourceReady = FROZEN_STATUSES.includes(processStatus ?? "");
  const unavailable = disabled || !isPdf || sourceReady || processStatus === "parsing";

  async function handleParse() {
    if (unavailable) {
      return;
    }

    setIsParsing(true);
    setMessage(null);
    setError(null);

    const result = await parseDocument(documentId);
    setIsParsing(false);

    if (!result.success) {
      if (result.error?.code === "DOCUMENT_ALREADY_PARSED") {
        setError("该文档已有来源或切片，不会重复解析或覆盖。");
        return;
      }

      setError(result.error?.message ?? "解析失败。");
      return;
    }

    setMessage(`清洗来源已保存，共 ${result.data?.character_count ?? 0} 个字符。图谱与检索切片尚未生成。`);
    router.refresh();
  }

  return (
    <div className="flex flex-col gap-3 rounded-md border border-slate-200 bg-white p-4 shadow-sm">
      <div>
        <h3 className="text-base font-semibold text-slate-950">解析与清洗</h3>
        <p className="mt-1 text-sm leading-6 text-slate-600">解析 PDF，清洗并保存固定版本的来源正文。此步骤不生成检索切片。</p>
      </div>

      <button
        type="button"
        onClick={handleParse}
        disabled={unavailable || isParsing}
        className="w-fit rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-400"
      >
        {isParsing || processStatus === "parsing" ? "解析中..." : disabled ? "删除状态下不可解析" : !isPdf ? "仅支持 PDF" : sourceReady ? "清洗来源已冻结" : "解析文档"}
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
