"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { processingRequest, type ProcessingJob, type ProcessingJobs } from "@/lib/document-processing";
import type { SegmentationConfig } from "@/lib/chunk-sets";

const stages: Record<string, string> = {
  uploaded: "等待解析", parsing: "解析与清洗", source_ready: "来源已冻结", kg_extracting: "知识抽取",
  kg_writing: "图谱写入与验证", kg_ready: "图谱完成", chunking: "顺序切分", chunks_ready: "切片完成",
  embedding: "向量化", indexing: "索引验证与发布", indexed: "已发布",
};
const statuses: Record<string, string> = {
  queued: "排队中", running: "执行中", failed: "失败，等待处理", cancelled: "已取消", succeeded: "成功", retry_wait: "等待重试",
};
type Pending = { request_id: string; config: SegmentationConfig };

export default function DocumentProcessingPanel({ documentId }: { documentId: string }) {
  const router = useRouter();
  const [data, setData] = useState<ProcessingJobs | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [config, setConfig] = useState<SegmentationConfig>({ chunk_size: 1200, overlap: 120, boundary: "paragraph" });
  const pending = useRef<Pending | null>(null);
  const knownStage = useRef<string | null>(null);
  const storageKey = `pdf-process-request:${documentId}`;

  const refresh = useCallback(async () => {
    const next = await processingRequest<ProcessingJobs>(documentId);
    setData(next);
    return next;
  }, [documentId]);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const next = await processingRequest<ProcessingJobs>(documentId);
        if (disposed) return;
        setData(next); setError(null);
        const stage = next.items.map(j => `${j.job_id}:${j.stage}:${j.status}`).join("|");
        if (knownStage.current !== null && knownStage.current !== stage) router.refresh();
        knownStage.current = stage;
        timer = setTimeout(poll, next.items.some(j => ["queued", "running", "retry_wait"].includes(j.status)) ? 2000 : 10000);
      } catch (value) {
        if (!disposed) {
          setError(value instanceof Error ? value.message : "任务状态不可用");
          timer = setTimeout(poll, 5000);
        }
      }
    }
    void poll();
    return () => { disposed = true; if (timer) clearTimeout(timer); };
  }, [documentId, router]);

  async function action(run: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(null);
    try { await run(); await refresh(); router.refresh(); }
    catch (value) { setError(value instanceof Error ? value.message : "请求结果未知，请先刷新状态。"); }
    finally { setBusy(false); }
  }

  async function start() {
    const next = await refresh(); // Network uncertainty is resolved before reposting.
    const stored = sessionStorage.getItem(storageKey);
    if (!pending.current && stored) pending.current = JSON.parse(stored) as Pending;
    if (pending.current && next.items.some(j => j.request_id === pending.current?.request_id)) {
      pending.current = null; sessionStorage.removeItem(storageKey); return;
    }
    if (!pending.current) {
      if (!Number.isInteger(config.chunk_size) || !Number.isInteger(config.overlap) || config.chunk_size < 1
        || config.chunk_size > 60000 || config.overlap < 0 || config.overlap >= config.chunk_size) {
        throw new Error("块大小须为 1–60000 的整数，重叠须小于块大小。");
      }
      pending.current = { request_id: crypto.randomUUID(), config };
      sessionStorage.setItem(storageKey, JSON.stringify(pending.current));
    }
    await processingRequest<ProcessingJob>(documentId, "/process", pending.current);
    pending.current = null; sessionStorage.removeItem(storageKey);
  }

  async function control(job: ProcessingJob, operation: "retry" | "cancel" | "resume") {
    const current = await processingRequest<ProcessingJob>(documentId, `/processing-jobs/${job.job_id}`);
    if (operation === "retry" && !current.can_retry) throw new Error("当前任务不可重试，请刷新状态。");
    await processingRequest(documentId, `/processing-jobs/${job.job_id}/${operation}`, operation === "resume" ? { config } : {});
  }

  return <section className="grid gap-3 rounded-md border border-slate-200 bg-white p-5 text-sm">
    <h2 className="text-xl font-semibold">PDF 完整处理</h2>
    <p>解析清洗并冻结来源 → 构图 → 顺序切分 → 向量化 → 索引发布。离开页面后任务仍由服务端执行。</p>
    <p>处理服务：{data?.executor_enabled ? "已启用" : "未启用"}；新版检索准入：{data?.search_enabled ? "已启用" : "未启用"}。</p>
    <div className="flex flex-wrap gap-3">
      <label>块大小 <input className="w-24 border p-1" type="number" value={config.chunk_size} min={1} max={60000} disabled={busy}
        onChange={e => setConfig({ ...config, chunk_size: Number(e.target.value) })} /></label>
      <label>重叠字符数 <input className="w-24 border p-1" type="number" value={config.overlap} min={0} disabled={busy}
        onChange={e => setConfig({ ...config, overlap: Number(e.target.value) })} /></label>
      <label>顺序切分边界 <select value={config.boundary} disabled={busy} className="border p-1"
        onChange={e => setConfig({ ...config, boundary: e.target.value as SegmentationConfig["boundary"] })}>
        <option value="paragraph">优先段落</option><option value="line">优先换行</option><option value="characters">固定字符数</option>
      </select></label>
    </div>
    <div className="flex gap-3">
      <button disabled={busy || !data?.can_process} className="rounded border px-3 py-2 disabled:opacity-50"
        onClick={() => void action(start)}>开始完整处理</button>
      <button disabled={busy} className="rounded border px-3 py-2" onClick={() => void action(async () => { await refresh(); })}>刷新任务状态</button>
    </div>
    {data?.items.map(job => <article key={job.job_id} className="grid gap-2 border-t pt-3">
      <p>{job.operation === "rechunk" ? "重切分" : "完整处理"} · {stages[job.stage] ?? job.stage} · {statuses[job.status] ?? job.status}</p>
      <p className="break-all text-slate-500">任务：{job.job_id}；已执行批次/尝试：{job.attempt_count}/{job.max_attempts}</p>
      <p>抽取单元：{job.completed_units}/{job.unit_count ?? "待生成"}；切片：{job.chunk_count ?? 0}；已向量化：{job.embedded_count}。</p>
      {job.graph_status === "ready_empty" ? <p>图谱抽取已成功完成，没有合格三元组；后续切片的图谱引用为空。</p> : null}
      {job.source_version ? <p className="break-all">来源版本：{job.source_version}</p> : null}
      {job.last_error_code ? <p role="status">诊断：{job.last_error_code}。失败版本不会替换已发布版本。</p> : null}
      {job.cancel_requested ? <p>已请求取消，等待正在执行的阶段结束。</p> : null}
      {job.requires_io_reconciliation ? <p>曾出现外部写入结果不确定或租约接管。删除前须单独核验相关任务已停止。</p> : null}
      <div className="flex gap-3">
        {job.managed && job.can_retry ? <button disabled={busy || !data.executor_enabled} className="rounded border px-3 py-2" onClick={() => void action(() => control(job, "retry"))}>显式重试</button> : null}
        {!job.managed && job.graph_build_id && ["queued", "failed"].includes(job.status) ? <button disabled={busy || !data.executor_enabled} className="rounded border px-3 py-2" onClick={() => void action(() => control(job, "resume"))}>接入后台处理{job.status === "failed" ? "（仍需显式重试）" : ""}</button> : null}
        {job.can_cancel ? <button disabled={busy || job.cancel_requested} className="rounded border px-3 py-2" onClick={() => void action(() => control(job, "cancel"))}>取消任务</button> : null}
      </div>
    </article>)}
    {error ? <p role="alert" className="text-amber-800">{error}</p> : null}
  </section>;
}
