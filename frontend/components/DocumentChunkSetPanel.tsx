"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { chunkSetRequest, type ChunkSets, type ChunkSetStatus, type SegmentationConfig } from "@/lib/chunk-sets";
import { processingRequest, type ProcessingJobs } from "@/lib/document-processing";

type Props = { documentId: string; initial: ChunkSets | null; errorMessage: string | null };

export default function DocumentChunkSetPanel({ documentId, initial, errorMessage }: Props) {
  const router = useRouter();
  const [data, setData] = useState(initial);
  const [selected, setSelected] = useState(initial?.items[0]?.chunk_set_id ?? "");
  const [config, setConfig] = useState<SegmentationConfig>({ chunk_size: 1200, overlap: 120, boundary: "paragraph" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(errorMessage);
  const pendingCreate = useRef<object | null>(null);
  const row = data?.items.find(item => item.chunk_set_id === selected);
  const stageNames: Record<string, string> = { kg_ready: "待切分", chunking: "切分中", chunks_ready: "切分完成",
    embedding: "向量化", indexing: "索引验证与发布", indexed: "已发布" };

  async function refresh() {
    const next = await chunkSetRequest<ChunkSets>(documentId);
    setData(next);
    return next;
  }

  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(null);
    try { await action(); router.refresh(); }
    catch (value) { setError(value instanceof Error ? value.message : "请求未完成，请先刷新状态。"); }
    finally { setBusy(false); }
  }

  async function create() {
    if (!pendingCreate.current && (!Number.isInteger(config.chunk_size) || !Number.isInteger(config.overlap)
      || config.chunk_size < 1 || config.chunk_size > 60000 || config.overlap < 0 || config.overlap >= config.chunk_size)) {
      throw new Error("块大小须为 1–60000 的整数，重叠须为小于块大小的非负整数。");
    }
    const next = await refresh(); // Read status before retrying an uncertain request.
    const failed = next.items.find(item => item.chunk_set_id === selected && item.job_status === "failed");
    const source = next.current ?? next.process_ready ?? failed;
    if (!source) throw new Error("请先完成图谱构建。");
    if (!pendingCreate.current) {
      const tasks = await processingRequest<ProcessingJobs>(documentId);
      pendingCreate.current = { source_version: source.source_version, graph_build_id: source.graph_build_id,
        request_id: next.current || failed ? crypto.randomUUID() : source.request_id,
        operation: next.current || failed ? "rechunk" : "process", config, auto_run: tasks.executor_enabled };
    }
    const result = await chunkSetRequest<ChunkSetStatus>(documentId, "", pendingCreate.current);
    pendingCreate.current = null;
    await refresh(); setSelected(result.chunk_set_id);
  }

  return <section className="grid gap-3 rounded-md border border-slate-200 bg-white p-4 text-sm">
    <h3 className="text-base font-semibold">切片版本与重切分</h3>
    <p>依次生成切片、向量并发布索引。重切分复用冻结来源和已有图谱，历史切片继续保留。</p>
    <p>当前检索版本：{data?.current?.chunk_set_id ?? "尚未发布"}。新版检索准入：{data?.current?.search_enabled ? "已启用" : "未启用"}。</p>
    <div className="flex flex-wrap gap-3">
      <label>块大小（字符） <input type="number" min={1} max={60000} value={config.chunk_size} disabled={busy}
        onChange={event => setConfig({ ...config, chunk_size: Number(event.target.value) })} className="w-24 border p-1" /></label>
      <label>重叠（字符） <input type="number" min={0} max={59999} value={config.overlap} disabled={busy}
        onChange={event => setConfig({ ...config, overlap: Number(event.target.value) })} className="w-24 border p-1" /></label>
      <label>边界 <select value={config.boundary} disabled={busy} className="border p-1"
        onChange={event => setConfig({ ...config, boundary: event.target.value as SegmentationConfig["boundary"] })}>
        <option value="paragraph">优先段落</option><option value="line">优先换行</option><option value="characters">固定字符数</option>
      </select></label>
    </div>
    <div className="flex gap-3">
      <button type="button" disabled={busy || !(data?.current || data?.process_ready || row?.job_status === "failed")} onClick={() => void run(create)} className="rounded border px-3 py-2 disabled:opacity-50">
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
