"use client";

import { useState } from "react";

import { vectorSearch, type VectorSearchData, type VectorSearchItem } from "@/lib/search";

function formatMetric(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 4,
  }).format(value);
}

function contentPreview(content: string): string {
  if (content.length <= 220) {
    return content;
  }

  return `${content.slice(0, 220)}...`;
}

function metadataText(metadata: Record<string, unknown> | null): string {
  if (!metadata || Object.keys(metadata).length === 0) {
    return "无";
  }

  return JSON.stringify(metadata, null, 2);
}

function SearchResultItem({ item }: { item: VectorSearchItem }) {
  return (
    <article className="border-t border-slate-100 px-5 py-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-xs font-medium text-slate-700">
              {item.original_filename}
            </span>
            <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600">
              chunk #{item.chunk_index}
            </span>
            <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600">
              {item.chunk_type ?? "unknown"}
            </span>
            <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600">
              {item.embedding_status}
            </span>
          </div>
          <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-slate-800">
            {contentPreview(item.content)}
          </p>
        </div>

        <div className="grid min-w-40 grid-cols-2 gap-2 text-sm">
          <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
            <p className="text-xs font-medium text-slate-500">distance</p>
            <p className="mt-1 font-semibold text-slate-950">{formatMetric(item.distance)}</p>
          </div>
          <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
            <p className="text-xs font-medium text-slate-500">score</p>
            <p className="mt-1 font-semibold text-slate-950">{formatMetric(item.score)}</p>
          </div>
        </div>
      </div>

      <dl className="mt-4 grid gap-3 text-xs text-slate-600 md:grid-cols-3">
        <div>
          <dt className="font-medium text-slate-500">document_id</dt>
          <dd className="mt-1 break-all text-slate-900">{item.document_id}</dd>
        </div>
        <div>
          <dt className="font-medium text-slate-500">embedding_model</dt>
          <dd className="mt-1 break-words text-slate-900">{item.embedding_model ?? "unknown"}</dd>
        </div>
        <div>
          <dt className="font-medium text-slate-500">embedding_dim</dt>
          <dd className="mt-1 break-words text-slate-900">{item.embedding_dim ?? "unknown"}</dd>
        </div>
      </dl>

      <pre className="mt-3 max-h-40 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3 text-xs leading-5 text-slate-700">
        {metadataText(item.source_metadata)}
      </pre>
    </article>
  );
}

export default function VectorSearchPanel() {
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(10);
  const [documentId, setDocumentId] = useState("");
  const [isSearching, setIsSearching] = useState(false);
  const [result, setResult] = useState<VectorSearchData | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSearch() {
    const trimmedQuery = query.trim();
    const trimmedDocumentId = documentId.trim();

    if (!trimmedQuery) {
      setError("请输入 query 后再检索。");
      setResult(null);
      return;
    }

    setIsSearching(true);
    setError(null);

    const response = await vectorSearch({
      query: trimmedQuery,
      limit,
      document_id: trimmedDocumentId || undefined,
    });
    setIsSearching(false);

    if (!response.success) {
      setResult(null);
      setError(response.error?.message ?? "向量检索失败。");
      return;
    }

    setResult(response.data);
  }

  return (
    <div className="rounded-md border border-slate-200 bg-white shadow-sm">
      <div className="grid gap-4 border-b border-slate-200 px-5 py-4 lg:grid-cols-[1fr_140px_1fr_auto] lg:items-end">
        <label className="grid gap-2">
          <span className="text-sm font-medium text-slate-700">Query</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="例如：冒口补缩设计"
            className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
          />
        </label>

        <label className="grid gap-2">
          <span className="text-sm font-medium text-slate-700">Limit</span>
          <input
            type="number"
            min={1}
            max={50}
            value={limit}
            onChange={(event) => setLimit(Number(event.target.value))}
            className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
          />
        </label>

        <label className="grid gap-2">
          <span className="text-sm font-medium text-slate-700">Document ID</span>
          <input
            value={documentId}
            onChange={(event) => setDocumentId(event.target.value)}
            placeholder="可选，限定单个文档"
            className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
          />
        </label>

        <button
          type="button"
          onClick={handleSearch}
          disabled={isSearching}
          className="rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-400"
        >
          {isSearching ? "检索中..." : "检索"}
        </button>
      </div>

      <div className="px-5 py-3 text-sm text-slate-600">
        distance 越小越相似；score = 1 - distance。
      </div>

      {error ? (
        <div className="mx-5 mb-4 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          {error}
        </div>
      ) : null}

      {result ? (
        <div>
          <div className="border-t border-slate-200 px-5 py-3 text-sm text-slate-700">
            命中 {result.total} 条，query: <span className="font-medium text-slate-950">{result.query}</span>
          </div>

          {result.items.length > 0 ? (
            <div>
              {result.items.map((item) => (
                <SearchResultItem key={item.chunk_id} item={item} />
              ))}
            </div>
          ) : (
            <div className="border-t border-slate-100 px-5 py-10 text-center text-sm text-slate-500">
              暂无命中，请先为文档生成 embedding 或更换查询。
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
