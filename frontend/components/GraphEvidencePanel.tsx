import type { RagGraphData, RagGraphEvidence } from "@/lib/rag";

function EvidenceRecord({ evidence }: { evidence: RagGraphEvidence }) {
  return (
    <article className="space-y-4 border-t border-slate-200 py-4 first:border-t-0 first:pt-0 last:pb-0">
      <div>
        <h4 className="break-words text-sm font-semibold text-slate-950">
          表格 {evidence.table_ref ?? evidence.table.table_ref}
          {evidence.table.page !== null && <span className="ml-2 font-normal text-slate-500">第 {evidence.table.page} 页</span>}
        </h4>
        <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-2">
          <div><dt className="text-slate-500">图谱</dt><dd className="mt-1 break-all text-slate-800">{evidence.graph_id}</dd></div>
          <div><dt className="text-slate-500">Anchor（{evidence.anchor_type}）</dt><dd className="mt-1 break-all text-slate-800">{evidence.anchor_id}</dd></div>
          <div><dt className="text-slate-500">图谱文档标识</dt><dd className="mt-1 break-all text-slate-800">{evidence.document.doc_id}</dd></div>
          <div><dt className="text-slate-500">Table 标识</dt><dd className="mt-1 break-all text-slate-800">{evidence.table.table_id}</dd></div>
        </dl>
        <p className="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-600">
          文本依据：
          {evidence.source_citations.map((id) => (
            <span key={id} className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 font-semibold text-slate-700">[{id}]</span>
          ))}
        </p>
      </div>

      <div>
        <h5 className="mb-2 text-xs font-semibold text-slate-700">实体 · 本次使用 {evidence.entities.length} 个</h5>
        {evidence.entities.length === 0 ? (
          <p className="text-xs leading-5 text-slate-500">本次上下文未保留实体记录。</p>
        ) : (
          <div role="region" aria-label={`${evidence.anchor_id} 实体列表`} tabIndex={0}
            className="max-h-80 overflow-auto rounded-md border border-slate-200 [scrollbar-gutter:stable] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-500">
            <table className="w-full table-fixed text-left text-xs leading-5">
              <caption className="sr-only">本次回答实际使用的实体</caption>
              <thead className="sticky top-0 bg-slate-50 text-slate-600">
                <tr><th scope="col" className="w-1/2 px-3 py-2 font-medium">名称</th><th scope="col" className="px-3 py-2 font-medium">类型</th><th scope="col" className="w-16 px-3 py-2 font-medium">页码</th></tr>
              </thead>
              <tbody className="divide-y divide-slate-100 text-slate-800">
                {evidence.entities.map((entity) => (
                  <tr key={entity.id}><td className="break-words px-3 py-2 align-top">{entity.name}</td><td className="break-words px-3 py-2 align-top">{entity.entity_type}</td><td className="px-3 py-2 align-top">{entity.page ?? "—"}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div>
        <h5 className="mb-2 text-xs font-semibold text-slate-700">关系 · 本次使用 {evidence.relationships.length} 条</h5>
        {evidence.relationships.length === 0 ? (
          <p className="text-xs leading-5 text-slate-500">本次上下文未保留关系记录。</p>
        ) : (
          <div role="region" aria-label={`${evidence.anchor_id} 关系列表`} tabIndex={0}
            className="max-h-80 overflow-auto rounded-md border border-slate-200 [scrollbar-gutter:stable] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-500">
            <table className="w-full table-fixed text-left text-xs leading-5">
              <caption className="sr-only">本次回答实际使用的关系</caption>
              <thead className="sticky top-0 bg-slate-50 text-slate-600">
                <tr><th scope="col" className="px-3 py-2 font-medium">源实体</th><th scope="col" className="w-20 px-3 py-2 font-medium">关系</th><th scope="col" className="px-3 py-2 font-medium">目标实体</th></tr>
              </thead>
              <tbody className="divide-y divide-slate-100 text-slate-800">
                {evidence.relationships.map((edge) => (
                  <tr key={JSON.stringify([edge.source_entity_id, edge.type, edge.target_entity_id])}>
                    <td className="break-words px-3 py-2 align-top">{edge.source_name}</td><td className="break-words px-3 py-2 align-top">{edge.type}</td><td className="break-words px-3 py-2 align-top">{edge.target_name}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </article>
  );
}

export default function GraphEvidencePanel({ graph }: { graph?: RagGraphData | null }) {
  if (!graph?.enabled || !graph.triggered || graph.status === "not_triggered") return null;

  return (
    <section aria-label="知识图谱检索结果" className="px-5 pb-4">
      <details className="rounded-md border border-slate-200 bg-white">
        <summary className="cursor-pointer rounded-md px-4 py-3 text-sm text-slate-950 marker:text-slate-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-500">
          <span className="font-semibold">知识图谱检索结果</span>
          <span className="ml-2 inline-block text-xs text-slate-500">本次使用 {graph.evidence_count} 个图谱锚点</span>
          {graph.truncated && <span className="ml-2 inline-block rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-800">已截断</span>}
          {graph.status === "partial" && <span className="ml-2 inline-block rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-800">部分可用</span>}
          {graph.status === "unavailable" && <span className="ml-2 inline-block rounded-md bg-slate-100 px-2 py-1 text-xs text-slate-600">暂不可用</span>}
        </summary>
        <div className="space-y-4 border-t border-slate-100 px-4 py-4">
          {graph.status === "unavailable" ? (
            <p className="text-sm leading-6 text-slate-600">知识图谱检索暂时不可用，本次回答基于文本证据生成。</p>
          ) : (
            <p className="text-xs leading-5 text-slate-500">图谱用于辅助理解结构与关系；数值、单位、范围及适用条件请核对文本依据。</p>
          )}
          {graph.status === "partial" && <p className="text-sm leading-6 text-amber-800">部分图谱证据未能获取。</p>}
          {graph.truncated && <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900">图谱证据已按本次回答上下文预算截断，以下仅展示实际用于本次回答的部分图谱结果。</p>}
          {graph.evidence.map((evidence) => <EvidenceRecord key={JSON.stringify([evidence.graph_id, evidence.anchor_id])} evidence={evidence} />)}
        </div>
      </details>
    </section>
  );
}
