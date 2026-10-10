"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { chunkSetRequest, type ChunkSets, type ChunkSetStatus, configError, readSegmentationDefaults, type BlockChunkerConfig } from "@/lib/chunk-sets";
import BlockChunkerConfigFields from "@/components/BlockChunkerConfigFields";
import { processingRequest, type ProcessingJobs } from "@/lib/document-processing";

type Props = { documentId: string; initial: ChunkSets | null; errorMessage: string | null };

export default function DocumentChunkSetPanel({ documentId, initial, errorMessage }: Props) {
  const router = useRouter();
  const [data, setData] = useState(initial);
  const [selected, setSelected] = useState(initial?.items[0]?.chunk_set_id ?? "");
  const [config, setConfig] = useState<BlockChunkerConfig | null>(() => {
    try { return initial ? readSegmentationDefaults(initial) : null; } catch { return null; }
  });
  const [contractReady, setContractReady] = useState(config !== null);
  const [busy, setBusy] = useState(false);
  const [hasPending, setHasPending] = useState(false);
  const [error, setError] = useState<string | null>(() => {
    if (errorMessage || !initial) return errorMessage;
    try { readSegmentationDefaults(initial); return null; }
    catch (value) { return value instanceof Error ? value.message : "后端切分配置不可用"; }
  });
  const pendingCreate = useRef<{ source_version: string; graph_build_id: string; request_id: string; operation: "process" | "rechunk"; config: BlockChunkerConfig; auto_run: boolean } | null>(null);
  const row = data?.items.find(item => item.chunk_set_id === selected);
  const stageNames: Record<string, string> = { kg_ready: "待切分", chunking: "切分中", chunks_ready: "切分完成",
    embedding: "向量化", indexing: "索引验证与发布", indexed: "已发布" };

  async function refresh() {
    try {
      const next = await chunkSetRequest<ChunkSets>(documentId);
      const defaults = readSegmentationDefaults(next);
      setData(next); setConfig(current => current ?? defaults); setContractReady(true);
      return next;
    } catch (value) { setContractReady(false); throw value; }
  }

  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(null);
    try { await action(); router.refresh(); }
    catch (value) { setError(value instanceof Error ? value.message : "请求未完成，请先刷新状态。"); }
    finally { setBusy(false); }
  }

  async function create() {
    const invalid = configError(config);
    if (!config || invalid) throw new Error(invalid ?? "切分配置尚未加载");
    const next = await refresh(); // Read status before retrying an uncertain request.
    const confirmed = next.items.find(item => item.request_id === pendingCreate.current?.request_id);
    if (confirmed) { pendingCreate.current = null; setHasPending(false); setSelected(confirmed.chunk_set_id); return; }
    const failed = next.items.find(item => item.chunk_set_id === selected && item.job_status === "failed");
    const source = next.current ?? next.process_ready ?? failed;
    if (!source) throw new Error("请先完成图谱构建。");
    if (!pendingCreate.current) {
      const tasks = await processingRequest<ProcessingJobs>(documentId);
      pendingCreate.current = { source_version: source.source_version, graph_build_id: source.graph_build_id,
        request_id: next.current || failed ? crypto.randomUUID() : source.request_id,
        operation: next.current || failed ? "rechunk" : "process", config: { ...config }, auto_run: tasks.executor_enabled };
      setHasPending(true);
    }
    const result = await chunkSetRequest<ChunkSetStatus>(documentId, "", pendingCreate.current);
    pendingCreate.current = null; setHasPending(false);
    await refresh(); setSelected(result.chunk_set_id);
  }

  return <section className="grid gap-3 rounded-md border border-slate-200 bg-white p-4 text-sm">
    <h3 className="text-base font-semibold">切片版本与重切分</h3>
    <p>依次生成切片、向量并发布索引。重切分复用冻结来源和已有图谱，历史切片继续保留。</p>
    <p>当前检索版本：{data?.current?.chunk_set_id ?? "尚未发布"}。新版检索准入：{data?.current?.search_enabled ? "已启用" : "未启用"}。</p>
    <BlockChunkerConfigFields value={config} onChange={setConfig} disabled={busy || hasPending || !contractReady} />
    {hasPending ? <p>待确认创建请求使用原请求 ID 和原配置，请先刷新状态。</p> : null}
    <div className="flex gap-3">
      <button type="button" disabled={busy || !contractReady || !config || !!configError(config) || !(data?.current || data?.process_ready || row?.job_status === "failed")} onClick={() => void run(create)} className="rounded border px-3 py-2 disabled:opacity-50">
        {data?.current || row?.job_status === "failed" ? "用当前规则创建重切分任务" : "创建切片任务"}
      </button>
      <button type="button" disabled={busy} onClick={() => void run(async () => { await refresh(); })} className="rounded border px-3 py-2">刷新状态</button>
    </div>
    {data?.items.length ? <label>最近版本（共 {data.total} 个） <select value={selected} className="max-w-full border p-1"
      onChange={event => setSelected(event.target.value)}>
      <option value="">选择版本</option>
      {data.items.map(item => <option key={item.chunk_set_id} value={item.chunk_set_id}>{item.is_current ? "当前 · " : ""}{item.chunk_set_id} · {item.status}</option>)}
    </select></label> : null}
    {row ? <div className="grid gap-2">
      <p className="break-all">切分版本：{row.segmentation_version}；配置指纹：{row.segmentation_config_sha256}</p>
      <p>冻结配置：正文 {row.segmentation_config.max_chunk_chars} / 尾块 {row.segmentation_config.min_chunk_chars} / 期望重叠 {row.segmentation_config.overlap_chars} / 表格 {row.segmentation_config.max_table_chars}；整表保护 {String(row.segmentation_config.keep_table_intact)}；公式合并 {String(row.segmentation_config.keep_formula_with_context)}。单步与重试使用此配置。</p>
      <p>阶段：{stageNames[row.stage] ?? row.stage}；任务：{row.job_status}；切片：{row.chunk_count ?? 0}；已向量化：{row.embedding_counts.embedded ?? 0}。</p>
      {row.last_error_code ? <p role="status">诊断：{row.last_error_code}。失败版本不会替换当前检索版本。</p> : null}
      <button type="button" disabled={busy || !row.can_advance} className="w-fit rounded border px-3 py-2 disabled:opacity-50" onClick={() => void run(async () => {
        await chunkSetRequest<ChunkSetStatus>(documentId, `/${row.chunk_set_id}/advance`, { retry: ["failed", "running"].includes(row.job_status) });
        await refresh();
      })}>{["failed", "running"].includes(row.job_status) ? "显式重试 / 恢复已过期任务" : "执行下一步"}</button>
      <p className="text-slate-500">{row.managed ? "此任务由后台执行，进度、取消与显式重试请使用上方任务面板。" : "每次执行一个阶段或一个向量批次。运行中的有效任务不可重复执行。"}</p>
    </div> : null}
    {error ? <p role="alert" className="text-amber-800">{error}</p> : null}
  </section>;
}
