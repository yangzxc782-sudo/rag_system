"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { clearCreation, creationRequestId } from "@/lib/qa-pending";
import { isAbort, mergeSessions, qaErrorMessage, qaSessions, type QaSession } from "@/lib/qa-sessions";

export default function QaSessionList({ threadId, revision, onRenamed }: {
  threadId?: string; revision: number; onRenamed: (session: QaSession) => void;
}) {
  const router = useRouter();
  const [items, setItems] = useState<QaSession[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [createFailed, setCreateFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [edit, setEdit] = useState<{ id: string; title: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const lifetime = useRef<AbortController | null>(null);
  const listing = useRef<AbortController | null>(null);
  const mutationBusy = useRef(false);
  const pendingCreation = useRef<string | null>(null);
  useEffect(() => {
    const control = new AbortController(); lifetime.current = control;
    return () => control.abort();
  }, []);
  useEffect(() => {
    const control = new AbortController(); listing.current = control;
    qaSessions.list(control.signal).then(({ data }) => {
      if (!control.signal.aborted) { setItems(data.items); setCursor(data.next_cursor); setError(null); }
    }).catch(e => { if (!isAbort(e)) setError(qaErrorMessage(e)); })
      .finally(() => { if (!control.signal.aborted) setLoading(false); });
    return () => control.abort();
  }, [threadId, revision, refresh]);
  async function create() {
    if (mutationBusy.current || !lifetime.current) return;
    mutationBusy.current = true;
    const signal = lifetime.current.signal, requestId = pendingCreation.current ?? creationRequestId();
    pendingCreation.current = requestId;
    setCreating(true); setError(null);
    try {
      const { data } = await qaSessions.create(requestId, signal);
      if (!signal.aborted) {
        clearCreation(requestId); pendingCreation.current = null; setCreateFailed(false); setItems(old => mergeSessions(old, [data]));
        router.push(`/rag/${data.thread_id}`);
      }
    } catch (e) { if (!isAbort(e)) { setError(qaErrorMessage(e)); setCreateFailed(true); } }
    finally { mutationBusy.current = false; if (!signal.aborted) setCreating(false); }
  }
  async function more() {
    if (loading || !cursor || !listing.current) return;
    const signal = listing.current.signal; setLoading(true);
    try {
      const { data } = await qaSessions.list(signal, cursor);
      if (!signal.aborted) { setItems(old => mergeSessions(old, data.items)); setCursor(data.next_cursor); }
    } catch (e) { if (!isAbort(e)) setError(qaErrorMessage(e)); }
    finally { if (!signal.aborted) setLoading(false); }
  }
  async function rename(event: FormEvent) {
    event.preventDefault();
    if (!edit?.title.trim() || saving || !lifetime.current) return;
    const signal = lifetime.current.signal; setSaving(true); setError(null);
    try {
      const { data } = await qaSessions.rename(edit.id, edit.title.trim(), signal);
      if (!signal.aborted) { setItems(old => mergeSessions(old, [data])); setEdit(null); onRenamed(data); }
    } catch (e) { if (!isAbort(e)) setError(qaErrorMessage(e)); }
    finally { if (!signal.aborted) setSaving(false); }
  }
  return <aside aria-label="历史会话" className="min-w-0 rounded-xl border border-slate-200 bg-white p-4">
    <div className="flex items-center justify-between gap-2"><h2 className="font-semibold">历史会话</h2>
      <button className="text-xs text-slate-600 underline disabled:opacity-50" disabled={loading} onClick={() => { setLoading(true); setRefresh(n => n + 1); }}>刷新列表</button></div>
    <button type="button" disabled={creating} onClick={() => void create()} className="my-4 w-full rounded-md bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">
      {creating ? "正在创建…" : createFailed ? "重试新建对话" : "新建对话"}</button>
    {error && <p role="alert" className="mb-3 text-xs leading-6 text-amber-800">{error}</p>}
    {items.length === 0 && <p className="py-3 text-sm text-slate-500">{loading ? "正在加载会话…" : "暂无会话，开始一段新的问答。"}</p>}
    <ul className="max-h-64 space-y-1 overflow-y-auto md:max-h-[60vh]" aria-label="会话列表">
      {items.map(item => <li key={item.thread_id} className={`rounded-md border p-2 ${item.thread_id === threadId ? "border-slate-300 bg-slate-100" : "border-transparent"}`}>
        {edit?.id === item.thread_id ? <form onSubmit={rename} className="space-y-2">
          <input autoFocus aria-label="会话标题" maxLength={255} required value={edit.title} onChange={e => setEdit({ id: item.thread_id, title: e.target.value })} className="w-full rounded border border-slate-300 bg-white p-2 text-sm" />
          <div className="flex gap-3 text-xs"><button disabled={saving} type="submit">保存标题</button><button disabled={saving} type="button" onClick={() => setEdit(null)}>取消改名</button></div>
        </form> : <div className="flex items-start gap-2">
          <Link href={`/rag/${item.thread_id}`} prefetch={false} aria-current={item.thread_id === threadId ? "page" : undefined} className="min-w-0 flex-1 break-words text-sm leading-6">{item.title}</Link>
          <button aria-label={`重命名 ${item.title}`} onClick={() => setEdit({ id: item.thread_id, title: item.title })} className="shrink-0 px-1 py-1 text-xs text-slate-500 hover:text-slate-950">改名</button>
        </div>}
      </li>)}
    </ul>
    {cursor && <button disabled={loading} onClick={() => void more()} className="mt-3 w-full rounded border border-slate-200 p-2 text-sm disabled:opacity-50">{loading ? "正在加载…" : "加载更多会话"}</button>}
  </aside>;
}
