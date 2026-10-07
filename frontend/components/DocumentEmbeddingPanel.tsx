"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import {
  generateDocumentEmbeddings,
  type DocumentEmbeddingData,
  type DocumentEmbeddingStatusData,
} from "@/lib/documents";
import {
  syncDocumentSearchIndex,
  type SearchIndexRebuildData,
} from "@/lib/search";

type DocumentEmbeddingPanelProps = {
  documentId: string;
  status: DocumentEmbeddingStatusData | null;
  errorMessage?: string | null;
  disabled?: boolean;
};

function formatList(values: Array<string | number>): string {
  if (values.length === 0) {
    return "none";
  }

  return values.join(", ");
}

function resultMessage(data: DocumentEmbeddingData): string {
  return `本次生成 ${data.embedded} 个，跳过 ${data.skipped} 个，失败 ${data.failed} 个。`;
}

function indexResultMessage(data: SearchIndexRebuildData): string {
  return `索引同步完成：符合条件 ${data.syncable_chunks} 个，写入 ${data.indexed} 个，移除旧记录 ${data.deleted} 个。`;
}

export default function DocumentEmbeddingPanel({
  documentId,
  status,
  errorMessage,
  disabled = false,
}: DocumentEmbeddingPanelProps) {
  const router = useRouter();
  const [isGenerating, setIsGenerating] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(errorMessage ?? null);
  const [isSyncingIndex, setIsSyncingIndex] = useState(false);
  const [indexMessage, setIndexMessage] = useState<string | null>(null);
  const [indexError, setIndexError] = useState<string | null>(null);

  async function handleGenerate() {
    if (disabled) {
      return;
    }

    setIsGenerating(true);
    setMessage(null);
    setError(null);
    setIndexMessage(null);
    setIndexError(null);

    const result = await generateDocumentEmbeddings(documentId);
    setIsGenerating(false);

    if (!result.success) {
      if (result.error?.code === "DOCUMENT_EMBEDDINGS_ALREADY_GENERATED") {
        setError("已全部生成 embedding，不会覆盖已有结果。");
        return;
      }

      if (result.error?.code === "DOCUMENT_NOT_PARSED") {
        setError("文档尚无检索切片。清洗来源完成后，还需完成构图与切分。");
        return;
      }

      setError(result.error?.message ?? "embedding 生成失败。");
      return;
    }

    setMessage(result.data ? resultMessage(result.data) : "embedding 生成完成。");
    if (result.data && result.data.embedded > 0) {
      setIndexError("已生成新的 embedding，请重新同步搜索索引。");
    }
    router.refresh();
  }

  async function handleSyncIndex() {
    if (disabled) {
      return;
    }

    setIsSyncingIndex(true);
    setIndexMessage(null);
    setIndexError(null);

    const result = await syncDocumentSearchIndex(documentId);
    setIsSyncingIndex(false);

    if (!result.success || !result.data) {
      setIndexError(result.error?.message ?? "搜索索引同步失败。");
      return;
    }

    setIndexMessage(indexResultMessage(result.data));
  }

  return (
    <div className="rounded-md border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h3 className="text-base font-semibold text-slate-950">Embedding 状态</h3>
          <p className="mt-1 text-sm leading-6 text-slate-600">
            基于已解析 chunks 生成本地 Qwen3 embedding，已生成的 chunks 默认跳过。
          </p>
        </div>

        <button
          type="button"
          onClick={handleGenerate}
          disabled={disabled || isGenerating || isSyncingIndex || status?.total === 0}
          className="w-fit rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-400"
        >
          {isGenerating ? "生成中..." : disabled ? "删除状态下不可生成" : status?.total === 0 ? "尚无检索切片" : "生成 embedding"}
        </button>
      </div>

      {status ? (
        <div className="mt-4 grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
            <p className="text-xs font-medium text-slate-500">total</p>
            <p className="mt-1 text-lg font-semibold text-slate-950">{status.total}</p>
          </div>
          <div className="rounded-md border border-slate-200 bg-white px-3 py-2">
            <p className="text-xs font-medium text-slate-500">not_started</p>
            <p className="mt-1 text-lg font-semibold text-slate-950">{status.not_started}</p>
          </div>
          <div className="rounded-md border border-slate-200 bg-white px-3 py-2">
            <p className="text-xs font-medium text-slate-500">embedding</p>
            <p className="mt-1 text-lg font-semibold text-slate-950">{status.embedding}</p>
          </div>
          <div className="rounded-md border border-slate-200 bg-white px-3 py-2">
            <p className="text-xs font-medium text-slate-500">embedded</p>
            <p className="mt-1 text-lg font-semibold text-slate-950">{status.embedded}</p>
          </div>
          <div className="rounded-md border border-slate-200 bg-white px-3 py-2">
            <p className="text-xs font-medium text-slate-500">embed_failed</p>
            <p className="mt-1 text-lg font-semibold text-slate-950">{status.embed_failed}</p>
          </div>
          <div className="rounded-md border border-slate-200 bg-white px-3 py-2">
            <p className="text-xs font-medium text-slate-500">dim</p>
            <p className="mt-1 text-lg font-semibold text-slate-950">{formatList(status.dims)}</p>
          </div>
        </div>
      ) : (
        <div className="mt-4 rounded-md border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600">
          暂无 embedding 状态。需要已有检索切片才能向量化。
        </div>
      )}

      {status ? (
        <p className="mt-3 break-words text-xs text-slate-500">models: {formatList(status.models)}</p>
      ) : null}

      {message ? (
        <p className="mt-3 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          {message}
        </p>
      ) : null}

      {error ? (
        <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          {error}
        </p>
      ) : null}

      <div className="mt-4 border-t border-slate-200 pt-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h4 className="text-sm font-semibold text-slate-950">OpenSearch 索引</h4>
            <p className="mt-1 text-sm text-slate-600">
              当前可同步 {status?.embedded ?? 0} 个 embedded chunks。
            </p>
          </div>
          <button
            type="button"
            onClick={handleSyncIndex}
            disabled={disabled || isSyncingIndex || isGenerating || !status || status.embedded === 0}
            className="w-fit rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-950 hover:bg-slate-50 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-400"
          >
            {isSyncingIndex ? "同步中..." : disabled ? "删除状态下不可同步" : "同步搜索索引"}
          </button>
        </div>

        {indexMessage ? (
          <p className="mt-3 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
            {indexMessage}
          </p>
        ) : null}

        {indexError ? (
          <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            {indexError}
          </p>
        ) : null}
      </div>
    </div>
  );
}
