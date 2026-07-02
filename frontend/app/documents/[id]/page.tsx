import Link from "next/link";

import AppShell from "@/components/AppShell";
import DocumentChunkList from "@/components/DocumentChunkList";
import DocumentDetailView from "@/components/DocumentDetail";
import DocumentEmbeddingPanel from "@/components/DocumentEmbeddingPanel";
import DocumentParseButton from "@/components/DocumentParseButton";
import { getDocument, getDocumentChunks, getDocumentEmbeddingStatus } from "@/lib/documents";

type DocumentDetailPageProps = {
  params: Promise<{
    id: string;
  }>;
};

export default async function DocumentDetailPage({ params }: DocumentDetailPageProps) {
  const { id } = await params;
  const documentResult = await getDocument(id);
  const document = documentResult.success ? documentResult.data : null;

  const chunksResult = document ? await getDocumentChunks(id, { limit: 50, offset: 0 }) : null;
  const chunks = chunksResult?.success ? chunksResult.data : null;
  const chunksErrorMessage =
    chunksResult && !chunksResult.success ? (chunksResult.error?.message ?? "文档切片暂时不可用。") : null;

  const embeddingStatusResult = document ? await getDocumentEmbeddingStatus(id) : null;
  const embeddingStatus = embeddingStatusResult?.success ? embeddingStatusResult.data : null;
  const embeddingStatusErrorMessage =
    embeddingStatusResult && !embeddingStatusResult.success
      ? (embeddingStatusResult.error?.message ?? "embedding 状态暂时不可用。")
      : null;

  return (
    <AppShell>
      <div className="grid gap-5">
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

        {document ? (
          <section className="grid gap-5">
            <DocumentParseButton documentId={document.id} />
            <DocumentEmbeddingPanel
              documentId={document.id}
              status={embeddingStatus}
              errorMessage={embeddingStatusErrorMessage}
            />

            <div className="grid gap-3">
              <div>
                <h2 className="text-xl font-semibold text-slate-950">切片结果</h2>
                <p className="mt-1 text-sm leading-6 text-slate-600">
                  查看第三阶段生成的基础 chunks、长度统计和来源元数据。
                </p>
              </div>
              <DocumentChunkList data={chunks} errorMessage={chunksErrorMessage} />
            </div>
          </section>
        ) : null}
      </div>
    </AppShell>
  );
}
