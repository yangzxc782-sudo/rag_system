"use client";
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type FormEvent } from "react";
import { ChatSession } from "@/lib/qa-chat";
import { qaErrorMessage } from "@/lib/qa-sessions";
import QaMessageList from "./QaMessageList";
import { CastingAttachments } from "@/lib/casting-attachments";
import CastingInputAttachment from "./CastingInputAttachment";

export default function RagChatPanel({ threadId, title, onCommitted }: { threadId: string; title?: string; onCommitted: () => void }) {
  const [question, setQuestion] = useState("");
  const [limit, setLimit] = useState(8);
  const [documentId, setDocumentId] = useState("");
  const [attachments] = useState(() => new CastingAttachments(threadId));
  const attachment = useSyncExternalStore(attachments.subscribe, attachments.getSnapshot, attachments.getServerSnapshot);
  // Parent keys by thread_id, including all temporary form and request state.
  const [chat] = useState(() => new ChatSession(threadId, () => {
    setQuestion(""); setLimit(8); setDocumentId(""); attachments.committed(); onCommitted();
  }));
  const state = useSyncExternalStore(chat.subscribe, chat.getSnapshot, chat.getServerSnapshot);
  const scroller = useRef<HTMLDivElement>(null);
  const anchor = useRef<{ height: number; top: number } | null>(null);
  const follow = useRef(true);
  useEffect(() => chat.connect(), [chat]);
  useEffect(() => attachments.connect(), [attachments]);
  useLayoutEffect(() => {
    const node = scroller.current;
    if (!node) return;
    if (anchor.current && !state.loadingOlder) {
      node.scrollTop = anchor.current.top + node.scrollHeight - anchor.current.height; anchor.current = null;
    } else if (!anchor.current && follow.current) node.scrollTop = node.scrollHeight;
  }, [state.messages, state.loadingOlder, state.working]);
  async function send(event: FormEvent) {
    event.preventDefault();
    if (!question.trim() || chat.blocked || attachments.blocked) return;
    follow.current = true;
    await chat.send(question, limit, documentId.trim() || null, attachment.selected?.file_id);
  }
  function older() {
    const node = scroller.current;
    if (node) { anchor.current = { height: node.scrollHeight, top: node.scrollTop }; follow.current = false; }
    void chat.loadOlder();
  }
  const active = state.status, unresolved = active && active.status !== "completed";
  return <section aria-label="当前对话" className="flex min-w-0 flex-col rounded-xl border border-slate-200 bg-white">
    <header className="flex items-start justify-between gap-3 border-b border-slate-100 p-4">
      <div className="min-w-0"><h2 className="break-words font-semibold">{title ?? state.session?.title ?? "加载会话"}</h2><p className="mt-1 text-xs leading-5 text-slate-500">回答保存后可随时回看；追问将沿用当前会话上下文。</p></div>
      <button disabled={state.loading || state.working || state.watching} onClick={() => void chat.reload()} className="shrink-0 text-xs text-slate-600 underline disabled:opacity-50">刷新历史</button>
    </header>
    <div ref={scroller} role="region" aria-label="聊天记录" tabIndex={0} onScroll={() => { const n = scroller.current; if (n) follow.current = n.scrollHeight - n.scrollTop - n.clientHeight < 80; }}
      className="h-[55vh] min-h-72 overflow-y-auto overscroll-contain p-4 [overflow-anchor:none] [scrollbar-gutter:stable] md:h-[60vh]">
      {state.before && <div className="mb-4 text-center"><button disabled={state.loadingOlder} onClick={older} className="rounded border border-slate-200 px-3 py-2 text-xs disabled:opacity-50">{state.loadingOlder ? "正在加载更早消息…" : "加载更早消息"}</button></div>}
      {state.loading && <p role="status" className="py-4 text-center text-sm text-slate-500">正在恢复历史…</p>}
      {!state.loading && state.messages.length === 0 && <div className="py-12 text-center text-sm leading-7 text-slate-500"><p>从一个具体的工艺问题开始。</p><p>例如：冒口有什么作用？</p></div>}
      <QaMessageList messages={state.messages} threadId={threadId} />
      {state.working && <p role="status" className="mt-5 rounded bg-slate-50 p-3 text-sm text-slate-600">正在处理请求，回答将完整返回…</p>}
    </div>
    <div className="border-t border-slate-100 p-4">
      {state.error && <p role="alert" className="mb-3 rounded bg-amber-50 p-3 text-sm leading-6 text-amber-900">{state.error}</p>}
      {(unresolved || state.pending) && <div className="mb-3 rounded-md border border-slate-200 bg-slate-50 p-3 text-sm leading-6">
        <p role="status">{state.watching ? "请求正在执行，正在查询进度…" : active?.status === "needs_recovery" ? "请求已中断，等待手动恢复。" : active?.status === "failed" ? "本轮执行失败。" : active?.status === "completed" ? "回答已保存。" : "正在核对本轮请求状态。"}</p>
        {state.pending && <p className="mt-1 break-words text-xs text-slate-600">本轮问题：{state.pending.question}</p>}
        {state.pending?.casting_input_file_id && <p className="break-all text-xs text-slate-600">原请求工程附件：{state.pending.casting_input_file_id}。重试沿用此文件。</p>}
        {active?.error_code && <p className="text-amber-900">{qaErrorMessage(active.error_code)}</p>}
        {state.paused && <p className="text-xs text-slate-500">自动查询已暂停。后端可能仍在执行，可手动查询状态。</p>}
        {unresolved && !active.can_retry && !active.execution_active && <p className="text-xs text-slate-500">此请求当前不允许重试。</p>}
        <div className="mt-2 flex flex-wrap gap-3"><button disabled={state.working || state.watching} onClick={() => void chat.check()} className="text-sm underline disabled:opacity-50">查询状态</button>
          {chat.canRetry && <button disabled={attachments.blocked} onClick={() => void chat.retry()} className="rounded bg-slate-900 px-3 py-1 text-sm text-white disabled:opacity-50">使用原请求重试</button>}</div>
      </div>}
      <form onSubmit={send} className="space-y-3">
        <label htmlFor="chat-question" className="sr-only">本轮问题</label>
        <textarea id="chat-question" value={question} onChange={e => setQuestion(e.target.value)} required maxLength={2000} disabled={chat.blocked} placeholder="输入问题，或继续追问…" rows={3}
          className="w-full resize-y rounded-lg border border-slate-300 p-3 text-sm leading-6 outline-none focus:border-slate-700 disabled:bg-slate-50 disabled:text-slate-400" />
        <CastingInputAttachment store={attachments} disabled={chat.blocked} />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <details className="min-w-0 text-xs text-slate-600"><summary className="cursor-pointer">本轮检索设置</summary>
            <div className="mt-2 flex flex-wrap gap-3">
              <label>检索条数<input aria-label="检索条数" type="number" min={1} max={50} required disabled={chat.blocked} value={limit} onChange={e => setLimit(Number(e.target.value))} className="ml-2 w-16 rounded border p-1" /></label>
              <label>限定文档（可选）<input aria-label="限定文档" value={documentId} disabled={chat.blocked} pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}" onChange={e => setDocumentId(e.target.value)} placeholder="文档 UUID" className="ml-2 w-48 max-w-full rounded border p-1" /></label>
            </div>
          </details>
          <button type="submit" disabled={chat.blocked || attachments.blocked || !question.trim()} className="rounded-md bg-slate-900 px-5 py-2 text-sm font-medium text-white disabled:opacity-40">发送问题</button>
        </div>
      </form>
    </div>
  </section>;
}
