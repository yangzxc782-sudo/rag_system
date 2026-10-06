import { useSyncExternalStore } from "react";
import type { CastingAttachments } from "@/lib/casting-attachments";
import type { FieldIssue } from "@/lib/qa-sessions";

export function CastingIssues({ issues }: { issues: FieldIssue[] }) {
  if (!issues.length) return null;
  return <ul aria-label="工程字段问题" className="mt-2 max-h-48 space-y-1 overflow-y-auto text-xs leading-5">
    {issues.map((issue, i) => <li key={i} className="break-words"><code>{issue.field_path}</code>：{issue.message} <span className="text-slate-500">（{issue.error_code}）</span></li>)}
  </ul>;
}

export default function CastingInputAttachment({ store, disabled }: { store: CastingAttachments; disabled: boolean }) {
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getServerSnapshot);
  return <div className="rounded-lg border border-slate-200 p-3 text-xs text-slate-600">
    <button type="button" aria-expanded={state.open} aria-controls="casting-attachment" onClick={() => store.open()} className="text-sm font-medium text-slate-700 underline">工程 JSON 附件</button>
    {state.open && <div id="casting-attachment" className="mt-3 space-y-3">
      <p>上传浇冒系统计算输入，最大 256 KiB。参数修改请更新 JSON 后重新上传。</p>
      <label className="block">选择 JSON 文件<input aria-label="选择 JSON 文件" type="file" accept=".json,application/json" disabled={disabled}
        onChange={event => { const file = event.target.files?.[0]; event.target.value = ""; if (file) store.choose(file); }}
        className="mt-1 block w-full min-w-0 text-xs file:mr-2 file:rounded file:border file:border-slate-300 file:bg-white file:px-3 file:py-2 disabled:opacity-50" /></label>
      {state.files.length > 0 && <label className="block">或选择本会话已上传文件<select aria-label="本会话已上传 JSON" disabled={disabled || state.uploading} value={state.selected?.file_id ?? ""}
        onChange={e => store.select(e.target.value)} className="mt-1 w-full rounded border border-slate-300 bg-white p-2">
        <option value="">本轮不指定新附件</option>
        {state.files.filter(f => f.storage_state === "ready").map(f => <option key={f.file_id} value={f.file_id}>{f.original_filename} · {f.file_id.slice(0, 8)}{f.admission_passed ? " · 已通过准入" : " · 待工程准入"}</option>)}
      </select></label>}
      {state.loading && <p role="status">正在读取工程文件…</p>}
      {state.listError && <p role="alert" className="rounded bg-amber-50 p-2 text-amber-900">{state.listError}</p>}
      {state.before && <button type="button" disabled={state.loading || disabled} onClick={() => void store.load(true)} className="underline">加载更多工程文件</button>}
      {state.filename && <div className="flex flex-wrap items-center gap-2 rounded bg-slate-50 p-2">
        <span className="min-w-0 break-all">{state.filename}</span>
        <span role="status">{state.uploading ? "正在上传与检查结构…" : state.selected ? "已选定，随本轮问题提交" : "尚未上传成功"}</span>
        <button type="button" disabled={disabled} onClick={() => store.clear()} className="underline disabled:opacity-50">移除本轮附件</button>
      </div>}
      {state.selected && <p className="break-all">文件编号：{state.selected.file_id}。结构检查通过，工程准入将在计算时执行。</p>}
      {!state.selected && <p className="break-words">{state.reusable
        ? `如需继续计算，后端可复用最近明确选定且已准入的输入：${state.reusable.original_filename}（${state.reusable.file_id}）。`
        : "未指定新附件时，后端会检查本会话是否有可复用的有效输入。"}</p>}
      {state.error && <div role="alert" className="rounded bg-amber-50 p-2 text-amber-900"><p>{state.error}</p><CastingIssues issues={state.issues} /></div>}
      {state.retryable && <button type="button" disabled={disabled || state.uploading} onClick={() => void store.upload()} className="underline">使用原文件重试上传</button>}
      <button type="button" disabled={state.loading || disabled} onClick={() => void store.load()} className="underline">刷新工程文件</button>
    </div>}
    {!state.open && state.filename && <p className="mt-2 break-all">{state.filename} · {state.selected ? "已选定" : "等待上传完成"}</p>}
  </div>;
}
