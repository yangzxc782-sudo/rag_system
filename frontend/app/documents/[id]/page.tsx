import Link from "next/link";

import AppShell from "@/components/AppShell";
import DocumentDetailView from "@/components/DocumentDetail";
import { getDocument } from "@/lib/documents";

type DocumentDetailPageProps = {
  params: Promise<{
    id: string;
  }>;
};

export default async function DocumentDetailPage({ params }: DocumentDetailPageProps) {
  const { id } = await params;
  const documentResult = await getDocument(id);
  const document = documentResult.success ? documentResult.data : null;

  return (
    <AppShell>
      <section className="rounded-md border border-slate-200 bg-white shadow-sm">
        <div className="flex flex-col gap-3 border-b border-slate-200 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-xl font-semibold text-slate-950">文档详情</h2>
            <p className="mt-1 text-sm leading-6 text-slate-600">查看原始文件的基础入库元数据。</p>
          </div>
          <Link className="text-sm font-medium text-slate-950 underline-offset-4 hover:underline" href="/documents">
            返回文档管理
          </Link>
        </div>

        {!documentResult.success ? (
          <div className="m-5 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            {documentResult.error?.message ?? "文档详情暂时不可用。"}
          </div>
        ) : null}

        {document ? (
          <DocumentDetailView document={document} />
        ) : documentResult.success ? (
          <div className="px-5 py-10 text-center text-sm text-slate-500">未找到文档详情。</div>
        ) : null}
      </section>
    </AppShell>
  );
}
