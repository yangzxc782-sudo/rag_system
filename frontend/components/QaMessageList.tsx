import RagAnswerPanel from "./RagAnswerPanel";
import type { QaMessage } from "@/lib/qa-sessions";

export default function QaMessageList({ messages, threadId }: { messages: QaMessage[]; threadId: string }) {
  return <ol className="space-y-5" aria-label="会话消息">
    {messages.map(message => <li key={message.message_id} data-message-id={message.message_id} data-sequence={message.sequence_no}
      className={message.role === "user" ? "ml-6 rounded-lg bg-slate-100 p-4 sm:ml-16" : "mr-2 rounded-lg border border-slate-200 bg-white p-4 sm:mr-8"}>
      <div className="mb-2 flex items-center gap-3 text-xs text-slate-500">
        <span className="font-semibold text-slate-700">{message.role === "user" ? "你" : message.role === "assistant" ? "知识助手" : "历史消息"}</span>
        <time dateTime={message.created_at}>{new Date(message.created_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</time>
        {message.role === "user" && message.status === "failed" && <span>本轮未完成</span>}
      </div>
      {message.role === "user" && (message.casting_input_file_id || message.effective_casting_input_file_id) && <p className="mb-2 break-all text-xs leading-5 text-slate-600">
        {message.casting_input_file_id ? "工程附件" : "会话输入参考"}：{message.casting_input_filename ?? "JSON 输入"} · {message.casting_input_file_id ?? message.effective_casting_input_file_id}
      </p>}
      {message.role === "assistant" && message.result ? <RagAnswerPanel result={message.result} threadId={threadId} />
        : <p className="whitespace-pre-wrap break-words text-sm leading-7">{message.content}</p>}
    </li>)}
  </ol>;
}
