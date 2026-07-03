import Link from "next/link";
import type { ReactNode } from "react";

const modules = [
  {
    name: "知识库管理",
    description: "预留知识库范围、分类和基础配置入口",
  },
  {
    name: "文档管理",
    description: "上传原始文档并查看基础入库状态",
    href: "/documents",
  },
  {
    name: "智能检索",
    description: "检索已同步到搜索索引的文档 chunks",
    href: "/search",
  },
  {
    name: "知识条目库",
    description: "预留抽取结果、版本和专家审核入口",
  },
  {
    name: "智能问答",
    description: "预留会话、消息和检索日志入口",
  },
  {
    name: "系统状态",
    description: "查看后端与基础服务健康状态",
  },
];

export default function AppShell({ children }: { children: ReactNode }) {
  return (
    <main className="min-h-screen bg-[#f6f7f9] text-slate-950">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-8 px-5 py-6 sm:px-8 lg:px-10">
        <header className="flex flex-col gap-4 border-b border-slate-200 pb-6 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <p className="text-sm font-medium text-slate-500">本地开发闭环</p>
            <h1 className="mt-2 text-3xl font-semibold leading-tight text-slate-950 sm:text-4xl">
              铸型工艺知识库 RAG 管理系统
            </h1>
          </div>
          <div className="rounded-md border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600">
            后端 API：{process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000"}
          </div>
        </header>

        <section aria-label="应用入口" className="grid gap-3 md:grid-cols-3 xl:grid-cols-6">
          {modules.map((module) => {
            const content = (
              <>
                <h2 className="text-base font-semibold text-slate-950">{module.name}</h2>
                <p className="mt-2 text-sm leading-6 text-slate-600">{module.description}</p>
              </>
            );

            return module.href ? (
              <Link
                key={module.name}
                href={module.href}
                className="rounded-md border border-slate-200 bg-white p-4 shadow-sm transition hover:border-slate-300 hover:shadow"
              >
                {content}
              </Link>
            ) : (
              <article key={module.name} className="rounded-md border border-slate-200 bg-white p-4 shadow-sm">
                {content}
              </article>
            );
          })}
        </section>

        {children}
      </div>
    </main>
  );
}
