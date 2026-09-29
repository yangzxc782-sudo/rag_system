"use client";
import { useCallback, useState } from "react";
import QaSessionList from "./QaSessionList";
import RagChatPanel from "./RagChatPanel";
import type { QaSession } from "@/lib/qa-sessions";

export default function RagWorkspace({ threadId }: { threadId?: string }) {
  const [revision, setRevision] = useState(0);
  const [renamed, setRenamed] = useState<QaSession | null>(null);
  const committed = useCallback(() => setRevision(n => n + 1), []);
  return <div className="grid items-start gap-4 md:grid-cols-[15rem_minmax(0,1fr)]">
    <QaSessionList threadId={threadId} revision={revision} onRenamed={setRenamed} />
    {threadId ? <RagChatPanel key={threadId} threadId={threadId} onCommitted={committed} title={renamed?.thread_id === threadId ? renamed.title : undefined} />
      : <section className="rounded-xl border border-slate-200 bg-white p-8 sm:p-12">
        <p className="text-xs font-semibold tracking-wider text-slate-500">铸型工艺 · 知识问答</p>
        <h2 className="mt-3 text-2xl font-semibold tracking-tight">从一个问题开始，逐步深入。</h2>
        <p className="mt-4 max-w-lg text-sm leading-7 text-slate-600">新建对话讨论工艺问题，或从左侧继续历史会话。每段对话独立保存，回答附带本轮知识库依据；刷新后仍可继续。</p>
        <div className="mt-8 border-t border-slate-100 pt-6 text-sm leading-7 text-slate-500"><p>冒口有什么作用？</p><p>它的尺寸应该怎么确定？</p><p className="mt-3 text-xs">证据不足时，助手会提示补充条件。工程参数请结合原文核对。</p></div>
      </section>}
  </div>;
}
