import AppShell from "@/components/AppShell";
import VectorSearchPanel from "@/components/VectorSearchPanel";

export default function SearchPage() {
  return (
    <AppShell>
      <div className="grid gap-5">
        <section className="rounded-md border border-slate-200 bg-white px-5 py-4 shadow-sm">
          <h2 className="text-xl font-semibold text-slate-950">智能检索</h2>
          <p className="mt-1 text-sm leading-6 text-slate-600">
            当前使用关键词检索和语义向量检索的混合召回，结果按 hybrid_score 排序；本阶段不生成 RAG
            回答。检索前需要先生成 embedding 并同步搜索索引。
          </p>
        </section>

        <VectorSearchPanel />
      </div>
    </AppShell>
  );
}
