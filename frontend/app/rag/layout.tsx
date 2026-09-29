import AppShell from "@/components/AppShell";

export default function RagLayout({ children }: { children: React.ReactNode }) {
  return <AppShell compactNavigation>
    <div className="mb-5 flex flex-wrap items-center justify-between gap-2">
      <h1 className="text-xl font-semibold text-slate-950">多轮知识问答</h1>
      <p className="text-xs text-slate-500">可信本机使用 · 独立会话 · 历史持久保存</p>
    </div>
    {children}
  </AppShell>;
}
