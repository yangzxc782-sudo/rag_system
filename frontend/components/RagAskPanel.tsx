"use client";

import type { FormEvent } from "react";
import { useState } from "react";
import GraphEvidencePanel from "@/components/GraphEvidencePanel";

import {
  friendlyRagErrorMessage,
  ragAsk,
  type RagAskData,
  type RagCitationItem,
} from "@/lib/rag";

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
    return 8;
  }

  return Math.min(50, Math.max(1, Math.trunc(value)));
}

function sourceBadgeClass(source: string | null | undefined): string {
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

function CitationItem({ citation }: { citation: RagCitationItem }) {
  const retrievalSource = citation.retrieval_source ?? "unknown";
  const sourceLabel = SOURCE_LABELS[retrievalSource] ?? retrievalSource;

  return (
    <article className="border-t border-slate-100 px-5 py-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-xs font-semibold text-slate-700">
              [{citation.citation_id}]
            </span>
            <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700">
              {citation.original_filename ?? "unknown"}
            </span>
            <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600">
              chunk #{citation.chunk_index ?? "-"}
            </span>
            <span className={`rounded-md border px-2 py-1 text-xs font-medium ${sourceBadgeClass(retrievalSource)}`}>
              {sourceLabel}
            </span>
          </div>

          <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-slate-800">
            {citation.content}
          </p>
        </div>

        <div className="grid min-w-48 gap-2 text-sm">
          <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
            <p className="text-xs font-medium text-slate-500">hybrid_score</p>
            <p className="mt-1 font-semibold text-slate-950">{formatMetric(citation.hybrid_score)}</p>
          </div>
        </div>
      </div>

      <dl className="mt-4 grid gap-3 text-xs text-slate-600 md:grid-cols-2">
        <div>
          <dt className="font-medium text-slate-500">document_id</dt>
          <dd className="mt-1 break-all text-slate-900">{citation.document_id}</dd>
        </div>
        <div>
          <dt className="font-medium text-slate-500">chunk_id</dt>
          <dd className="mt-1 break-all text-slate-900">{citation.chunk_id}</dd>
        </div>
      </dl>
    </article>
  );
}

function AnswerPanel({ result }: { result: RagAskData }) {
  const isNoContext = result.context_status === "no_context";

  return (
    <div className="border-t border-slate-200">
      <div className="grid gap-3 px-5 py-4 md:grid-cols-4">
        <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm">
          <p className="text-xs font-medium text-slate-500">context_status</p>
          <p className={`mt-1 font-semibold ${isNoContext ? "text-amber-700" : "text-emerald-700"}`}>
            {result.context_status}
          </p>
        </div>
        <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm">
          <p className="text-xs font-medium text-slate-500">retrieval.total</p>
          <p className="mt-1 font-semibold text-slate-950">{result.retrieval.total}</p>
        </div>
        <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm">
          <p className="text-xs font-medium text-slate-500">llm.provider</p>
          <p className="mt-1 break-words font-semibold text-slate-950">{result.llm.provider ?? "-"}</p>
        </div>
        <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm">
          <p className="text-xs font-medium text-slate-500">llm.model</p>
          <p className="mt-1 break-words font-semibold text-slate-950">{result.llm.model ?? "-"}</p>
        </div>
      </div>

      <section className="px-5 pb-4">
        {isNoContext ? (
          <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900">
            {result.answer}
          </div>
        ) : (
          <div className="rounded-md border border-slate-200 bg-white px-4 py-3">
            <h3 className="text-sm font-semibold text-slate-950">回答</h3>
            <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-7 text-slate-800">{result.answer}</p>
          </div>
        )}
      </section>

      <GraphEvidencePanel graph={result.graph} />

      <section>
        <div className="border-t border-slate-200 px-5 py-3">
          <h3 className="text-sm font-semibold text-slate-950">引用片段</h3>
          <p className="mt-1 text-xs leading-5 text-slate-500">
            引用编号来自本次检索片段，不是文献编号。
          </p>
        </div>

        {result.citations.length > 0 ? (
          <div>
            {result.citations.map((citation) => (
              <CitationItem key={`${citation.citation_id}-${citation.chunk_id}`} citation={citation} />
            ))}
          </div>
        ) : (
          <div className="border-t border-slate-100 px-5 py-8 text-center text-sm text-slate-500">
            当前没有可展示的引用片段。
          </div>
        )}
      </section>
    </div>
  );
}

export default function RagAskPanel() {
  const [question, setQuestion] = useState("");
  const [limit, setLimit] = useState(8);
  const [isAsking, setIsAsking] = useState(false);
  const [result, setResult] = useState<RagAskData | null>(null);
  const [answerVersion, setAnswerVersion] = useState(0);
  const [error, setError] = useState<string | null>(null);

  async function handleAsk(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const trimmedQuestion = question.trim();
    const normalizedLimit = normalizeLimit(limit);
    setLimit(normalizedLimit);

    if (!trimmedQuestion) {
      setError("请输入问题。");
      setResult(null);
      return;
    }

    setIsAsking(true);
    setError(null);

    const response = await ragAsk({
      question: trimmedQuestion,
      limit: normalizedLimit,
    });
    setIsAsking(false);

    if (!response.success) {
      setResult(null);
      setError(friendlyRagErrorMessage(response.error));
      return;
    }

    if (!response.data) {
      setResult(null);
      setError("后端未返回问答结果。");
      return;
    }

    setResult(response.data);
    setAnswerVersion((version) => version + 1);
  }

  return (
    <div className="rounded-md border border-slate-200 bg-white shadow-sm">
      <form onSubmit={handleAsk} className="grid gap-4 border-b border-slate-200 px-5 py-4">
        <label className="grid gap-2">
          <span className="text-sm font-medium text-slate-700">问题</span>
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="例如：冒口如何保证热节补缩？"
            rows={4}
            className="resize-y rounded-md border border-slate-300 px-3 py-2 text-sm leading-6 outline-none focus:border-slate-500"
          />
        </label>

        <div className="grid gap-4 lg:grid-cols-[140px_auto] lg:items-end">
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
            disabled={isAsking}
            className="rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-400"
          >
            {isAsking ? "生成中..." : "提交问题"}
          </button>
        </div>
      </form>

      <div className="grid gap-2 px-5 py-3 text-sm leading-6 text-slate-600 md:grid-cols-3">
        <p>当前为单轮知识问答。</p>
        <p>回答基于当前知识库检索片段。</p>
        <p>暂不支持聊天历史或流式输出。</p>
      </div>

      {error ? (
        <div className="mx-5 mb-4 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          {error}
        </div>
      ) : null}

      {result ? <AnswerPanel key={answerVersion} result={result} /> : null}
    </div>
  );
}
