import GraphEvidencePanel from "@/components/GraphEvidencePanel";
import type { ConversationAnswer } from "@/lib/qa-sessions";

export default function RagAnswerPanel({ result }: { result: ConversationAnswer }) {
  const missing = result.sources.filter(source => source.status !== "available");
  const hidden = new Set(missing.filter(s => s.kind === "citation").map(s => s.citation_id));
  const citations = result.citations.filter(c => !hidden.has(c.citation_id));
  const visible = new Set(citations.map(c => c.citation_id));
  const graph = result.graph ? { ...result.graph,
    evidence: result.graph.evidence.filter(e => e.source_citations.every(id => visible.has(id))) } : null;
  if (graph) graph.evidence_count = graph.evidence.length;
  return <div className="min-w-0">
    <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
      <span className="rounded bg-slate-100 px-2 py-1 text-slate-700">
        {result.context_status === "clarification" ? "需要澄清" : result.context_status === "no_context" ? "暂无充分依据" : "知识库回答"}
      </span>
      {result.llm.model && <span>模型：{result.llm.model}{result.llm.provider ? ` · ${result.llm.provider}` : ""}</span>}
    </div>
    <p className="my-4 whitespace-pre-wrap break-words text-sm leading-7 text-slate-800">{result.answer}</p>
    {missing.length > 0 && <div className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-xs leading-6 text-amber-900">
      <p>原回答已保留，部分证据现已不可用。</p>
      {missing.map(source => <p key={source.snapshot_id}>
        {source.kind === "citation" ? `引用 [${source.citation_id}]` : "图谱证据"}：
        {source.status === "source_deleted" ? "来源已删除" : "来源暂不可用"}，不再展示摘录或相关图谱。
      </p>)}
    </div>}
    <GraphEvidencePanel graph={graph} />
    {citations.length > 0 && <details className="rounded-md border border-slate-200">
      <summary className="cursor-pointer p-3 text-sm font-medium focus-visible:outline-2">文本依据 · {citations.length} 条</summary>
      <div className="divide-y divide-slate-100">
        {citations.map(c => <article key={`${c.citation_id}-${c.chunk_id}`} className="p-4" aria-label={`引用 ${c.citation_id}`}>
          <p className="text-xs font-medium text-slate-600">[{c.citation_id}] {c.original_filename ?? "知识库文档"}
            {c.chunk_index != null && ` · 片段 ${c.chunk_index}`}</p>
          <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-slate-700">{c.content}</p>
        </article>)}
      </div>
    </details>}
  </div>;
}
