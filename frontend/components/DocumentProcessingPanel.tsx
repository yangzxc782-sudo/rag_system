"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { processingRequest, readPendingProcessing, type PendingProcessing, type ProcessingJob, type ProcessingJobs } from "@/lib/document-processing";
import { configError, readSegmentationDefaults, type BlockChunkerConfig } from "@/lib/chunk-sets";
import BlockChunkerConfigFields from "@/components/BlockChunkerConfigFields";

const stages: Record<string, string> = {
  uploaded: "等待解析", parsing: "解析与清洗", source_ready: "来源已冻结", kg_extracting: "知识抽取",
  kg_writing: "图谱写入与验证", kg_ready: "图谱完成", chunking: "结构感知切分", chunks_ready: "切片完成",
  embedding: "向量化", indexing: "索引验证与发布", indexed: "已发布",
};
const statuses: Record<string, string> = {
  queued: "排队中", running: "执行中", failed: "失败，等待处理", cancelled: "已取消", succeeded: "成功", retry_wait: "等待重试",
};
export default function DocumentProcessingPanel({ documentId }: { documentId: string }) {
  const router = useRouter();
  const [data, setData] = useState<ProcessingJobs | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [config, setConfig] = useState<BlockChunkerConfig | null>(null);
  const [cacheError, setCacheError] = useState<string | null>(null);
  const [hasPending, setHasPending] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const pending = useRef<PendingProcessing | null>(null);
  const knownStage = useRef<string | null>(null);
  const storageKey = `pdf-process-request:${documentId}`;

  const refresh = useCallback(async () => {
    try {
      const next = await processingRequest<ProcessingJobs>(documentId);
      const defaults = readSegmentationDefaults(next);
      setData(next); setConfig(current => current ?? defaults); setLoadError(null);
      return next;
    } catch (value) { setData(null); throw value; }
  }, [documentId]);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const next = await processingRequest<ProcessingJobs>(documentId);
        if (disposed) return;
        const defaults = readSegmentationDefaults(next);
        setData(next); setLoadError(null);
        const stored = sessionStorage.getItem(storageKey);
        if (stored) {
          try {
            const cached = readPendingProcessing(stored, next.segmentation_version);
            pending.current = cached; setHasPending(true); setConfig(cached.config); setCacheError(null);
          } catch (value) { setCacheError(value instanceof Error ? value.message : "缓存请求不可用"); }
        } else { setConfig(current => current ?? defaults); }

        const stage = next.items.map(j => `${j.job_id}:${j.stage}:${j.status}`).join("|");
        if (knownStage.current !== null && knownStage.current !== stage) router.refresh();
        knownStage.current = stage;
        timer = setTimeout(poll, next.items.some(j => ["queued", "running", "retry_wait"].includes(j.status)) ? 2000 : 10000);
      } catch (value) {
        if (!disposed) {
          setData(null); setLoadError(value instanceof Error ? value.message : "任务状态不可用");
          timer = setTimeout(poll, 5000);
        }
      }
    }
    void poll();
    return () => { disposed = true; if (timer) clearTimeout(timer); };
  }, [documentId, router, storageKey]);

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
    if (!pending.current && stored) pending.current = readPendingProcessing(stored, next.segmentation_version);
    if (pending.current && next.items.some(j => j.request_id === pending.current?.request_id)) {
      pending.current = null; setHasPending(false); sessionStorage.removeItem(storageKey); return;
    }
    if (!pending.current) {
      const invalid = configError(config);
      if (invalid || !config) throw new Error(invalid ?? "切分配置尚未加载");
      pending.current = { request_id: crypto.randomUUID(), config: { ...config }, segmentation_version: next.segmentation_version };
      sessionStorage.setItem(storageKey, JSON.stringify(pending.current)); setHasPending(true);
    }
    const { request_id, config: frozen } = pending.current;
    await processingRequest<ProcessingJob>(documentId, "/process", { request_id, config: frozen });
    pending.current = null; setHasPending(false); sessionStorage.removeItem(storageKey);
  }

  async function control(job: ProcessingJob, operation: "retry" | "cancel" | "resume") {
    const current = await processingRequest<ProcessingJob>(documentId, `/processing-jobs/${job.job_id}`);
    if (operation === "retry" && !current.can_retry) throw new Error("当前任务不可重试，请刷新状态。");
    await processingRequest(documentId, `/processing-jobs/${job.job_id}/${operation}`, operation === "resume" && !current.chunk_set_id && !current.config ? { config } : {});
  }

  return <section className="grid gap-3 rounded-md border border-slate-200 bg-white p-5 text-sm">
    <h2 className="text-xl font-semibold">PDF 完整处理</h2>
    <p>解析清洗并冻结来源 → 构图 → 结构感知切分 → 向量化 → 索引发布。离开页面后任务仍由服务端执行。</p>
    <p>处理服务：{data?.executor_enabled ? "已启用" : "未启用"}；新版检索准入：{data?.search_enabled ? "已启用" : "未启用"}。</p>
    <BlockChunkerConfigFields value={config} onChange={setConfig} disabled={busy || !data || hasPending || !!cacheError} />
    {hasPending ? <p>有待确认请求；先查询任务，再使用同一请求 ID 和冻结配置确认提交结果。</p> : null}
    {cacheError ? <div role="alert" className="text-amber-800"><p>{cacheError}</p>
      <button type="button" disabled={busy} className="rounded border px-3 py-2" onClick={() => {
        sessionStorage.removeItem(storageKey); pending.current = null; setHasPending(false); setCacheError(null);
        if (data) setConfig(readSegmentationDefaults(data));
      }}>丢弃不支持的缓存请求</button></div> : null}
    <div className="flex gap-3">
      <button disabled={busy || !data || (!data.can_process && !hasPending) || !config || !!configError(config) || !!cacheError} className="rounded border px-3 py-2 disabled:opacity-50"
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
    {loadError ? <p role="alert" className="text-amber-800">{loadError}</p> : null}
    {error ? <p role="alert" className="text-amber-800">{error}</p> : null}
  </section>;
}
