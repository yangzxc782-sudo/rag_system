"use client";

import type { FormEvent } from "react";
import { useState } from "react";

import {
  hybridSearch,
  type ApiError,
  type HybridSearchData,
  type HybridSearchItem,
} from "@/lib/search";

const SOURCE_LABELS: Record<string, string> = {
  both: "关键词 + 语义均命中",
  keyword: "关键词命中",
  vector: "语义命中",
};

function formatMetric(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }

  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 6,
  }).format(value);
}

function normalizeLimit(value: number): number {
  if (!Number.isFinite(value)) {
    return 10;
  }

  return Math.min(50, Math.max(1, Math.trunc(value)));
}

function contentPreview(content: string): string {
  if (content.length <= 360) {
    return content;
  }

  return `${content.slice(0, 360)}...`;
}

function metadataText(metadata: Record<string, unknown> | null): string {
  if (!metadata || Object.keys(metadata).length === 0) {
    return "无";
  }

  return JSON.stringify(metadata, null, 2);
}

function sourceBadgeClass(source: string): string {
  if (source === "both") {
    return "border-emerald-200 bg-emerald-50 text-emerald-800";
  }

  if (source === "keyword") {
    return "border-sky-200 bg-sky-50 text-sky-800";
  }

  if (source === "vector") {
    return "border-violet-200 bg-violet-50 text-violet-800";
  }

  return "border-slate-200 bg-slate-50 text-slate-700";
}

function friendlyErrorMessage(error: ApiError | null): string {
  switch (error?.code) {
    case "SEARCH_QUERY_EMPTY":
      return "请输入检索内容后再发起智能检索。";
    case "SEARCH_ENGINE_UNAVAILABLE":
      return "搜索索引服务不可用，请检查 OpenSearch 服务或配置。";
    case "SEARCH_INDEX_NOT_FOUND":
      return "搜索索引尚未创建，请先由管理员创建并同步索引。";
    case "SEARCH_INDEX_EMPTY":
      return "搜索索引为空，请先执行索引同步或确认 chunks 已生成 embedding。";
    case "HYBRID_SEARCH_FAILED":
      return "混合检索失败，请检查搜索索引配置或稍后重试。";
    case "FRONTEND_REQUEST_FAILED":
      return "网络请求失败，请确认后端服务可访问。";
    default:
      return error?.message ?? "智能检索失败，请稍后重试。";
  }
}

function SearchResultItem({ item }: { item: HybridSearchItem }) {
  const sourceLabel = SOURCE_LABELS[item.retrieval_source] ?? item.retrieval_source;

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
            <span className={`rounded-md border px-2 py-1 text-xs font-medium ${sourceBadgeClass(item.retrieval_source)}`}>
              {sourceLabel}
            </span>
          </div>
          <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-slate-800">
            {contentPreview(item.content)}
          </p>
        </div>

        <div className="grid min-w-60 grid-cols-3 gap-2 text-sm">
          <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
            <p className="text-xs font-medium text-slate-500">hybrid_score</p>
            <p className="mt-1 font-semibold text-slate-950">{formatMetric(item.hybrid_score)}</p>
          </div>
          <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
            <p className="text-xs font-medium text-slate-500">keyword_score</p>
            <p className="mt-1 font-semibold text-slate-950">{formatMetric(item.keyword_score)}</p>
          </div>
          <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
            <p className="text-xs font-medium text-slate-500">vector_score</p>
            <p className="mt-1 font-semibold text-slate-950">{formatMetric(item.vector_score)}</p>
          </div>
        </div>
      </div>

      <dl className="mt-4 grid gap-3 text-xs text-slate-600 md:grid-cols-4">
        <div>
          <dt className="font-medium text-slate-500">document_id</dt>
          <dd className="mt-1 break-all text-slate-900">{item.document_id}</dd>
        </div>
        <div>
          <dt className="font-medium text-slate-500">keyword_rank</dt>
          <dd className="mt-1 break-words text-slate-900">{item.keyword_rank ?? "-"}</dd>
        </div>
        <div>
          <dt className="font-medium text-slate-500">vector_rank</dt>
          <dd className="mt-1 break-words text-slate-900">{item.vector_rank ?? "-"}</dd>
        </div>
        <div>
          <dt className="font-medium text-slate-500">embedding</dt>
          <dd className="mt-1 break-words text-slate-900">
            {item.embedding_model ?? "unknown"} / {item.embedding_dim ?? "unknown"}
          </dd>
        </div>
      </dl>

      <div className="mt-4">
        <p className="text-xs font-medium text-slate-500">matched_keywords</p>
        {item.matched_keywords.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-2">
            {item.matched_keywords.map((keyword) => (
              <span key={keyword} className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700">
                {keyword}
              </span>
            ))}
          </div>
        ) : (
          <p className="mt-1 text-xs text-slate-500">无</p>
        )}
      </div>

      <pre className="mt-3 max-h-40 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3 text-xs leading-5 text-slate-700">
        {metadataText(item.source_metadata)}
      </pre>
    </article>
  );
}

export default function VectorSearchPanel() {
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(10);
  const [isSearching, setIsSearching] = useState(false);
  const [result, setResult] = useState<HybridSearchData | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const trimmedQuery = query.trim();
    const normalizedLimit = normalizeLimit(limit);
    setLimit(normalizedLimit);

    if (!trimmedQuery) {
      setError("请输入检索内容后再发起智能检索。");
      setResult(null);
      return;
    }

    setIsSearching(true);
    setError(null);

    const response = await hybridSearch({
      query: trimmedQuery,
      limit: normalizedLimit,
    });
    setIsSearching(false);

    if (!response.success) {
      setResult(null);
      setError(friendlyErrorMessage(response.error));
      return;
    }

    if (!response.data) {
      setResult(null);
      setError("后端未返回检索结果。");
      return;
    }

    setResult(response.data);
  }

  return (
    <div className="rounded-md border border-slate-200 bg-white shadow-sm">
      <form
        onSubmit={handleSearch}
        className="grid gap-4 border-b border-slate-200 px-5 py-4 lg:grid-cols-[1fr_140px_auto] lg:items-end"
      >
        <label className="grid gap-2">
          <span className="text-sm font-medium text-slate-700">检索内容</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="例如：冒口设计如何保证热节补缩"
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

        <button
          type="submit"
          disabled={isSearching}
          className="rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-400"
        >
          {isSearching ? "检索中..." : "智能检索"}
        </button>
      </form>

      <div className="px-5 py-3 text-sm leading-6 text-slate-600">
        当前使用 IK 中文分词关键词检索与语义向量检索的混合检索，排序依据为 hybrid_score；
        keyword_score / vector_score 仅用于展示，当前不生成 RAG 回答。
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
              暂无命中，请先执行索引同步或确认 chunks 已生成 embedding。
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
