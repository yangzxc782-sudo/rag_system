import AppShell from "@/components/AppShell";
import VectorSearchPanel from "@/components/VectorSearchPanel";

export default function SearchPage() {
  return (
    <AppShell>
      <div className="grid gap-5">
        <section className="rounded-md border border-slate-200 bg-white px-5 py-4 shadow-sm">
          <h2 className="text-xl font-semibold text-slate-950">向量检索</h2>
          <p className="mt-1 text-sm leading-6 text-slate-600">
            使用已生成 embedding 的 chunks 做基础向量检索；当前不生成 RAG 回答。distance 越小越相似。
          </p>
        </section>

        <VectorSearchPanel />
      </div>
    </AppShell>
  );
}
