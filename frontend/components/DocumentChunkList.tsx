"use client";

import { useState } from "react";

import type { DocumentChunkListData, DocumentChunkRead } from "@/lib/documents";

type DocumentChunkListProps = {
  data: DocumentChunkListData | null;
  errorMessage?: string | null;
};

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 1,
  }).format(value);
}

function metadataValue(chunk: DocumentChunkRead, key: string): string {
  const value = chunk.source_metadata?.[key];

  if (value === null || value === undefined) {
    return "未知";
  }

  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }

  return String(value);
}

function contentPreview(content: string): string {
  if (content.length <= 180) {
    return content;
  }

  return `${content.slice(0, 180)}...`;
}

export default function DocumentChunkList({ data, errorMessage }: DocumentChunkListProps) {
  const [expandedChunkId, setExpandedChunkId] = useState<string | null>(null);

  if (errorMessage) {
    return (
      <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
        {errorMessage}
      </div>
    );
  }

  if (!data || data.items.length === 0) {
    return (
      <div className="rounded-md border border-slate-200 bg-white px-5 py-10 text-center text-sm text-slate-500">
        暂无检索切片。解析清洗只保存来源，构图完成后才能生成切片。
      </div>
    );
  }

  return (
    <div className="rounded-md border border-slate-200 bg-white shadow-sm">
      <div className="grid gap-3 border-b border-slate-200 px-5 py-4 sm:grid-cols-5">
        <div>
          <p className="text-xs font-medium text-slate-500">Chunks</p>
          <p className="mt-1 text-lg font-semibold text-slate-950">{data.stats.chunk_count}</p>
        </div>
        <div>
          <p className="text-xs font-medium text-slate-500">总字符</p>
          <p className="mt-1 text-lg font-semibold text-slate-950">{formatNumber(data.stats.total_characters)}</p>
        </div>
        <div>
          <p className="text-xs font-medium text-slate-500">最短</p>
          <p className="mt-1 text-lg font-semibold text-slate-950">{formatNumber(data.stats.min_characters)}</p>
        </div>
        <div>
          <p className="text-xs font-medium text-slate-500">最长</p>
          <p className="mt-1 text-lg font-semibold text-slate-950">{formatNumber(data.stats.max_characters)}</p>
        </div>
        <div>
          <p className="text-xs font-medium text-slate-500">平均</p>
          <p className="mt-1 text-lg font-semibold text-slate-950">{formatNumber(data.stats.avg_characters)}</p>
        </div>
      </div>

      <div className="divide-y divide-slate-100">
        {data.items.map((chunk) => {
          const isExpanded = expandedChunkId === chunk.id;
          const characterCount = chunk.character_count ?? chunk.content.length;

          return (
            <article key={chunk.id} className="px-5 py-4">
              <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-xs font-medium text-slate-700">
                      #{chunk.chunk_index}
                    </span>
                    <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600">
                      {characterCount} 字符
                    </span>
                    <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600">
                      {chunk.chunk_type ?? "未知类型"}
                    </span>
                    <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600">
                      {chunk.embedding_status}
                    </span>
                  </div>
                  <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-slate-800">
                    {isExpanded ? chunk.content : contentPreview(chunk.content)}
                  </p>
                </div>

                <button
                  type="button"
                  onClick={() => setExpandedChunkId(isExpanded ? null : chunk.id)}
                  className="w-fit rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-900 hover:bg-slate-50"
                >
                  {isExpanded ? "收起" : "展开"}
                </button>
              </div>

              <dl className="mt-4 grid gap-2 text-xs text-slate-600 sm:grid-cols-5">
                <div>
                  <dt className="font-medium text-slate-500">parser</dt>
                  <dd className="mt-1 break-words text-slate-900">{metadataValue(chunk, "parser_name")}</dd>
                </div>
                <div>
                  <dt className="font-medium text-slate-500">version</dt>
                  <dd className="mt-1 break-words text-slate-900">{metadataValue(chunk, "parser_version")}</dd>
                </div>
                <div>
                  <dt className="font-medium text-slate-500">source</dt>
                  <dd className="mt-1 break-words text-slate-900">{metadataValue(chunk, "source_type")}</dd>
                </div>
                <div>
                  <dt className="font-medium text-slate-500">placeholder</dt>
                  <dd className="mt-1 break-words text-slate-900">{metadataValue(chunk, "placeholder")}</dd>
                </div>
                <div>
                  <dt className="font-medium text-slate-500">extension</dt>
                  <dd className="mt-1 break-words text-slate-900">{metadataValue(chunk, "original_extension")}</dd>
                </div>
              </dl>
            </article>
          );
        })}
      </div>
    </div>
  );
}
