import type { RagGraphData, RagGraphEvidence } from "@/lib/rag-evidence";

const reasons: Record<string, string> = {
  used: "已纳入回答上下文",
  incomplete_coverage: "当前文本未完整覆盖条款或表格，事实未使用",
  query_failed: "未取得完整可用的图谱结果",
  graph_budget: "超出图谱上下文预算，事实未使用",
  source_invalid: "来源校验未通过",
  prompt_omitted: "最终提示词未使用",
};
const queryStates: Record<string, string> = {
  success: "成功", not_found: "无结果", ambiguous: "身份冲突", conflicting_ref: "锚点冲突",
  timeout: "超时", unavailable: "不可用", truncated: "结果超限", disabled: "未启用",
  limit_exceeded: "锚点数量超限", budget_exhausted: "时间预算耗尽",
};
const sourceErrors: Record<string, string> = {
  v2_admission_disabled: "新版来源准入尚未启用",
  authority_unavailable: "来源验证暂不可用",
  source_invalid: "来源或证据版本不受支持",
  source_changed: "查询期间来源发布状态已变化",
};

function Properties({ value }: { value: Record<string, unknown> }) {
  if (!Object.keys(value).length) return null;
  return <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words text-xs text-slate-600">
    {JSON.stringify(value, null, 2)}
  </pre>;
}

function EvidenceRecord({ evidence }: { evidence: RagGraphEvidence }) {
  const { anchor, source } = evidence;
  return <article className="space-y-3 border-t border-slate-200 py-4">
    <h4 className="break-words text-sm font-semibold">
      {anchor.anchor_type === "table" ? "表格 " + (anchor.table_no ?? anchor.table_ref) : "条款 " + anchor.clause_ref}
      {anchor.heading && <span className="ml-2 font-normal">{anchor.heading}</span>}
    </h4>
    <p className="text-xs text-slate-600">文本依据：{evidence.source_citations.map(id => "[" + id + "]").join(" ")}</p>
    <details className="break-all text-xs text-slate-500">
      <summary className="cursor-pointer">来源与版本</summary>
      <p>文档：{source.document_id}</p><p>来源版本：{source.source_version}</p>
      <p>构图版本：{source.graph_build_id}</p><p>连续来源区间：[{source.source_start}, {source.source_end})</p>
      <p>图谱：{anchor.graph_id}</p><p>锚点：{anchor.anchor_id}</p>
    </details>
    <div role="region" aria-label="图谱实体与关系" tabIndex={0} className="max-h-96 overflow-auto rounded border border-slate-200 p-3">
      <h5 className="text-xs font-semibold">实体 · {evidence.entities.length} 个</h5>
      <ul className="divide-y divide-slate-100">
        {evidence.entities.map(entity => <li key={entity.id} className="py-2 text-sm">
          {entity.name} <span className="text-xs text-slate-500">（{entity.entity_type}）</span>
          <Properties value={entity.properties} />
        </li>)}
      </ul>
      <h5 className="mt-3 text-xs font-semibold">关系 · {evidence.relationships.length} 条</h5>
      <ul className="divide-y divide-slate-100">
        {evidence.relationships.map(edge => <li key={edge.id} className="py-2 text-sm">
          {edge.source_name} → {edge.type} → {edge.target_name}
          <Properties value={edge.properties} />
        </li>)}
      </ul>
    </div>
  </article>;
}

export default function GraphEvidencePanel({ graph }: { graph?: RagGraphData | null }) {
  if (!graph?.enabled || (!graph.triggered && !graph.source_error)) return null;
  return <section aria-label="知识图谱检索结果" className="px-5 pb-4">
    <details className="rounded-md border border-slate-200 bg-white">
      <summary className="cursor-pointer px-4 py-3 text-sm font-semibold">
        知识图谱检索结果 · {graph.diagnostics.filter(d => d.mapped).length} 个映射锚点 · {graph.evidence_count} 个纳入回答上下文
      </summary>
      <div className="space-y-3 border-t border-slate-100 px-4 py-4">
        <p className="text-xs leading-5 text-slate-500">图谱辅助理解结构和关系；数值、单位、范围与条件仍以本次保留的文本为依据。映射或查询成功不代表事实已用于回答。</p>
        {graph.source_error && <p className="text-sm text-amber-800">{sourceErrors[graph.source_error] ?? "图谱来源校验不可用"}，本次仅使用文本证据。</p>}
        {graph.truncated && <p className="text-xs text-amber-800">部分完整图谱单元因预算限制未进入回答上下文。</p>}
        <ul className="space-y-2 text-xs text-slate-600">
          {graph.diagnostics.map(d => <li key={JSON.stringify([d.graph_id, d.anchor_id])} className="break-all">
            {d.anchor_id}：映射{d.mapped ? "成功" : "失败"}；查询{queryStates[d.query_status] ?? "不可用"}；
            {reasons[d.use_status] ?? "事实未使用"}
          </li>)}
        </ul>
        {graph.evidence.map(e => <EvidenceRecord key={JSON.stringify([e.anchor.graph_id, e.anchor.anchor_id])} evidence={e} />)}
      </div>
    </details>
  </section>;
}
