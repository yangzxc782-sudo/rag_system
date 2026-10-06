"use client";
import { useEffect, useRef, useState } from "react";
import { castingDesign, castingStatusLabel, type CastingAnswerInfo } from "@/lib/casting-design";
import { qaErrorMessage, safeIssues } from "@/lib/qa-sessions";
import { CastingIssues } from "./CastingInputAttachment";

export default function CastingAnswerDetails({ info, threadId }: { info: CastingAnswerInfo; threadId?: string }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  async function download() {
    if (busy || !threadId || !info.run_id) return;
    const controller = new AbortController(); active.current = controller;
    setBusy(true); setError(null);
    try { await castingDesign.download(threadId, info.run_id, controller.signal); }
    catch { if (!controller.signal.aborted) setError(qaErrorMessage("CASTING_DOWNLOAD_FAILED")); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  return <div aria-label="工程计算来源" className="mt-3 rounded border border-slate-200 bg-slate-50 p-3 text-xs leading-6 text-slate-600">
    <p className="font-medium text-slate-800">{castingStatusLabel[info.result_status]}</p>
    {info.input_file_id && <p className="break-all">{info.route === "explain_existing" ? "读取历史结果的输入" : info.input_reused ? "复用会话有效输入" : "本轮指定输入"}：{info.input_file_id}</p>}
    {info.rule_id && <p className="break-words">规则：{info.rule_id} · 版本 {info.rule_version}</p>}
    {info.run_id && <p className="break-all">运行编号：{info.run_id}</p>}
    {info.candidate_rank != null && <p>当前查看第 {info.candidate_rank} 个候选；推荐排序仍以原计算结果为准。</p>}
    {info.summary_mode === "template" && info.run_id && <p>说明方式：程序结果模板。</p>}
    <CastingIssues issues={safeIssues(info.error)} />
    {threadId && info.run_id && info.result_file_id && <button type="button" disabled={busy} onClick={() => void download()} className="mt-2 underline disabled:opacity-50">{busy ? "正在下载…" : "下载 recommendation.json"}</button>}
    {error && <p role="alert" className="text-amber-900">{error}</p>}
  </div>;
}
