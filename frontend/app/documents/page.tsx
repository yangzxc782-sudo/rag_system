import AppShell from "@/components/AppShell";
import DocumentTable from "@/components/DocumentTable";
import DocumentUploadForm from "@/components/DocumentUploadForm";
import { getDocuments } from "@/lib/documents";

export default async function DocumentsPage() {
  const documentsResult = await getDocuments({ limit: 20, offset: 0 });
  const documents = documentsResult.success && documentsResult.data ? documentsResult.data.items : [];

  return (
    <AppShell>
      <section className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="rounded-md border border-slate-200 bg-white shadow-sm">
          <div className="border-b border-slate-200 px-5 py-4">
            <h2 className="text-xl font-semibold text-slate-950">文档管理</h2>
            <p className="mt-1 text-sm leading-6 text-slate-600">
              上传原始工艺资料并查看基础入库状态。
            </p>
          </div>

          {!documentsResult.success ? (
            <div className="m-5 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
              {documentsResult.error?.message ?? "文档列表暂时不可用。"}
            </div>
          ) : null}

          <DocumentTable documents={documents} />
        </div>

        <DocumentUploadForm />
      </section>
    </AppShell>
  );
}
