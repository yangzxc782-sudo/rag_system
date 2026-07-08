import AppShell from "@/components/AppShell";
import RagAskPanel from "@/components/RagAskPanel";

export default function RagPage() {
  return (
    <AppShell>
      <div className="grid gap-5">
        <section className="rounded-md border border-slate-200 bg-white px-5 py-4 shadow-sm">
          <h2 className="text-xl font-semibold text-slate-950">知识问答</h2>
          <p className="mt-1 text-sm leading-6 text-slate-600">
            当前为基于知识库检索结果的单轮 RAG 问答。回答由本地大语言模型生成，并返回引用片段；若知识库依据不足，会提示无法可靠回答。
            当前不支持多轮对话和流式输出。
          </p>
        </section>

        <RagAskPanel />
      </div>
    </AppShell>
  );
}
