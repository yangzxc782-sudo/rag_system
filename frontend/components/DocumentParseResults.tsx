"use client";

import { useState } from "react";

import {
  getDocumentAssets,
  getDocumentBlocks,
  type DocumentAssetRead,
  type DocumentBlockRead,
  type DocumentParseRunListData,
  type DocumentParseRunRead,
  type DocumentParseStatusRead,
  type PaginatedDocumentAssets,
  type PaginatedDocumentBlocks,
  type ParseOutputStatus,
} from "@/lib/documents";

type DocumentParseResultsProps = {
  documentId: string;
  status: DocumentParseStatusRead | null;
  parseRuns: DocumentParseRunListData | null;
  blocks: PaginatedDocumentBlocks | null;
  assets: PaginatedDocumentAssets | null;
  errorMessage?: string | null;
};

function formatDate(value: string | null): string {
  if (!value) {
    return "未记录";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date);
}

function outputStatusLabel(status: ParseOutputStatus): string {
  if (status === "saved") {
    return "已保存";
  }
  if (status === "download_deferred") {
    return "待下载";
  }
  return "不可用";
}

function OutputStatus({ status }: { status: ParseOutputStatus }) {
  const tone =
    status === "saved"
      ? "border-emerald-200 bg-emerald-50 text-emerald-800"
      : status === "download_deferred"
        ? "border-amber-200 bg-amber-50 text-amber-800"
        : "border-slate-200 bg-slate-50 text-slate-600";

  return (
    <span className={`inline-flex rounded-md border px-2 py-1 text-xs font-medium ${tone}`}>
      {outputStatusLabel(status)}
    </span>
  );
}

function metadataPreview(metadata: Record<string, unknown>): string {
  const text = JSON.stringify(metadata, null, 2);
  return text.length > 1600 ? `${text.slice(0, 1600)}...` : text;
}

function blockPreview(block: DocumentBlockRead): string {
  const content = block.markdown ?? block.text ?? block.latex ?? block.caption ?? block.html ?? "无可展示文本";
  return content.length > 500 ? `${content.slice(0, 500)}...` : content;
}

function pageRange(start: number | null, end: number | null): string {
  if (start === null && end === null) {
    return "页码未知";
  }
  if (start === null) {
    return `第 ${end} 页`;
  }
  if (start === end || end === null) {
    return `第 ${start ?? end} 页`;
  }
  return `第 ${start}-${end} 页`;
}

function ParseRunSummary({ run, label }: { run: DocumentParseRunRead; label: string }) {
  return (
    <div className="border-t border-slate-100 py-4 first:border-t-0">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-sm font-semibold text-slate-950">{label}</p>
        <span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-xs text-slate-700">
          {run.status}
        </span>
        {run.is_active ? (
          <span className="rounded-md border border-sky-200 bg-sky-50 px-2 py-1 text-xs font-medium text-sky-800">
            active
          </span>
        ) : null}
      </div>
      <dl className="mt-3 grid gap-x-5 gap-y-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <dt className="text-xs font-medium text-slate-500">Provider</dt>
          <dd className="mt-1 break-words text-slate-900">{run.parser_provider}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-slate-500">Mode</dt>
          <dd className="mt-1 text-slate-900">{run.parse_mode ?? "未记录"}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-slate-500">Blocks / Assets / Pages</dt>
          <dd className="mt-1 text-slate-900">
            {run.block_count ?? 0} / {run.asset_count ?? 0} / {run.page_count ?? 0}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-slate-500">完成时间</dt>
          <dd className="mt-1 text-slate-900">{formatDate(run.completed_at)}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-slate-500">创建时间</dt>
          <dd className="mt-1 text-slate-900">{formatDate(run.created_at)}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-slate-500">开始时间</dt>
          <dd className="mt-1 text-slate-900">{formatDate(run.started_at)}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-slate-500">output.md</dt>
          <dd className="mt-1">
            <OutputStatus status={run.output_markdown_status} />
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-slate-500">output.json</dt>
          <dd className="mt-1">
            <OutputStatus status={run.output_json_status} />
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-slate-500">失败状态持久化</dt>
          <dd className="mt-1 text-slate-900">
            {run.failure_status_persisted === null
              ? "未报告"
              : run.failure_status_persisted
                ? "已持久化"
                : "未持久化"}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-slate-500">Run ID</dt>
          <dd className="mt-1 break-all text-slate-900">{run.id}</dd>
        </div>
      </dl>
      {run.error_message ? (
        <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          {run.error_message}
        </p>
      ) : null}
      <details className="mt-3 text-xs text-slate-600">
        <summary className="cursor-pointer font-medium text-slate-700">产物 key 与任务摘要</summary>
        <dl className="mt-2 grid gap-2">
          <div>
            <dt className="font-medium text-slate-500">output.md key</dt>
            <dd className="mt-1 break-all text-slate-800">{run.output_markdown_key ?? "未生成"}</dd>
          </div>
          <div>
            <dt className="font-medium text-slate-500">output.json key</dt>
            <dd className="mt-1 break-all text-slate-800">{run.output_json_key ?? "未生成"}</dd>
          </div>
        </dl>
        {Object.keys(run.source_metadata_summary).length > 0 ? (
          <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-50 p-3">
            {metadataPreview(run.source_metadata_summary)}
          </pre>
        ) : null}
      </details>
    </div>
  );
}

function BlockRow({ block }: { block: DocumentBlockRead }) {
  return (
    <article className="py-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-slate-950">#{block.block_index}</span>
        <span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-xs text-slate-700">
          {block.block_type}
        </span>
        <span className="text-xs text-slate-500">{pageRange(block.page_start, block.page_end)}</span>
        {block.content_truncated ? <span className="text-xs text-amber-700">API 已截断</span> : null}
      </div>
      <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-slate-700">
        {blockPreview(block)}
      </p>
      <p className="mt-2 text-xs text-slate-500">
        {block.section_path.length > 0 ? block.section_path.join(" / ") : "未识别章节"}
      </p>
      {Object.keys(block.source_metadata_summary).length > 0 ? (
        <details className="mt-2 text-xs text-slate-600">
          <summary className="cursor-pointer font-medium text-slate-700">来源摘要</summary>
          <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-50 p-3">
            {metadataPreview(block.source_metadata_summary)}
          </pre>
        </details>
      ) : null}
    </article>
  );
}

function AssetRow({ asset }: { asset: DocumentAssetRead }) {
  return (
    <article className="py-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-slate-950">{asset.filename ?? "未命名资产"}</span>
        <span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-xs text-slate-700">
          {asset.asset_type}
        </span>
        <span className="text-xs text-slate-500">
          {asset.page_number !== null ? `第 ${asset.page_number} 页` : "页码未知"}
        </span>
      </div>
      <p className="mt-2 break-all text-xs text-slate-500">{asset.asset_key}</p>
      {asset.caption ? <p className="mt-2 text-sm leading-6 text-slate-700">{asset.caption}</p> : null}
      {Object.keys(asset.source_metadata_summary).length > 0 ? (
        <details className="mt-2 text-xs text-slate-600">
          <summary className="cursor-pointer font-medium text-slate-700">资产摘要</summary>
          <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-50 p-3">
            {metadataPreview(asset.source_metadata_summary)}
          </pre>
        </details>
      ) : null}
    </article>
  );
}

export default function DocumentParseResults({
  documentId,
  status,
  parseRuns,
  blocks: initialBlocks,
  assets: initialAssets,
  errorMessage,
}: DocumentParseResultsProps) {
  const selectedRun =
    status?.active_parse_run ?? status?.latest_parse_run ?? parseRuns?.items[0] ?? null;
  const [blocks, setBlocks] = useState(initialBlocks);
  const [assets, setAssets] = useState(initialAssets);
  const [loadingKind, setLoadingKind] = useState<"blocks" | "assets" | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [previousInput, setPreviousInput] = useState({ blocks: initialBlocks, assets: initialAssets, runId: selectedRun?.id });
  // Reset derived pagination before rendering new props, not in a cascading effect.
  if (previousInput.blocks !== initialBlocks || previousInput.assets !== initialAssets || previousInput.runId !== selectedRun?.id) {
    setPreviousInput({ blocks: initialBlocks, assets: initialAssets, runId: selectedRun?.id });
    setBlocks(initialBlocks);
    setAssets(initialAssets);
    setLoadError(null);
  }

  async function loadMoreBlocks() {
    if (!blocks || loadingKind) {
      return;
    }
    setLoadingKind("blocks");
    setLoadError(null);
    const result = await getDocumentBlocks(documentId, {
      parseRunId: selectedRun?.id,
      limit: blocks.limit,
      offset: blocks.items.length,
    });
    setLoadingKind(null);
    if (!result.success || !result.data) {
      setLoadError(result.error?.message ?? "结构块加载失败。");
      return;
    }
    setBlocks({
      ...result.data,
      items: [...blocks.items, ...result.data.items],
      offset: 0,
    });
  }

  async function loadMoreAssets() {
    if (!assets || loadingKind) {
      return;
    }
    setLoadingKind("assets");
    setLoadError(null);
    const result = await getDocumentAssets(documentId, {
      parseRunId: selectedRun?.id,
      limit: assets.limit,
      offset: assets.items.length,
    });
    setLoadingKind(null);
    if (!result.success || !result.data) {
      setLoadError(result.error?.message ?? "解析资产加载失败。");
      return;
    }
    setAssets({
      ...result.data,
      items: [...assets.items, ...result.data.items],
      offset: 0,
    });
  }

  return (
    <section className="rounded-md border border-slate-200 bg-white shadow-sm">
      <div className="border-b border-slate-200 px-5 py-4">
        <h2 className="text-lg font-semibold text-slate-950">解析中间层</h2>
        <p className="mt-1 text-sm leading-6 text-slate-600">MinerU 解析任务、结构块与资产元数据。</p>
      </div>

      {errorMessage ? (
        <p className="m-5 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          {errorMessage}
        </p>
      ) : null}

      <div className="px-5">
        {selectedRun ? (
          <>
            <p className="pt-4 text-sm text-slate-600">
              文档状态：{status?.process_status ?? "未知"}
            </p>
            <ParseRunSummary run={selectedRun} label={selectedRun.is_active ? "Active parse run" : "Latest parse run"} />
          </>
        ) : (
          <p className="py-8 text-center text-sm text-slate-500">暂无解析任务。</p>
        )}

        {parseRuns && parseRuns.items.length > 1 ? (
          <details className="border-t border-slate-100 py-4">
            <summary className="cursor-pointer text-sm font-semibold text-slate-900">
              历史解析任务（{parseRuns.total}）
            </summary>
            <div className="mt-3 divide-y divide-slate-100">
              {parseRuns.items.slice(0, 10).map((run) => (
                <ParseRunSummary key={run.id} run={run} label={formatDate(run.created_at)} />
              ))}
            </div>
          </details>
        ) : null}

        <details className="border-t border-slate-100 py-4" open>
          <summary className="cursor-pointer text-sm font-semibold text-slate-900">
            结构块（{blocks?.total ?? 0}）
          </summary>
          {blocks && blocks.items.length > 0 ? (
            <div className="mt-3 divide-y divide-slate-100">
              {blocks.items.map((block) => (
                <BlockRow key={block.id} block={block} />
              ))}
              {blocks.items.length < blocks.total ? (
                <button
                  type="button"
                  onClick={loadMoreBlocks}
                  disabled={loadingKind !== null}
                  className="my-3 rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-900 disabled:text-slate-400"
                >
                  {loadingKind === "blocks" ? "加载中..." : "加载更多结构块"}
                </button>
              ) : null}
            </div>
          ) : (
            <p className="py-6 text-sm text-slate-500">暂无结构块。</p>
          )}
        </details>

        <details className="border-t border-slate-100 py-4">
          <summary className="cursor-pointer text-sm font-semibold text-slate-900">
            解析资产（{assets?.total ?? 0}）
          </summary>
          {assets && assets.items.length > 0 ? (
            <div className="mt-3 divide-y divide-slate-100">
              {assets.items.map((asset) => (
                <AssetRow key={asset.id} asset={asset} />
              ))}
              {assets.items.length < assets.total ? (
                <button
                  type="button"
                  onClick={loadMoreAssets}
                  disabled={loadingKind !== null}
                  className="my-3 rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-900 disabled:text-slate-400"
                >
                  {loadingKind === "assets" ? "加载中..." : "加载更多解析资产"}
                </button>
              ) : null}
            </div>
          ) : (
            <p className="py-6 text-sm text-slate-500">暂无解析资产。</p>
          )}
        </details>

        {loadError ? (
          <p className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            {loadError}
          </p>
        ) : null}
      </div>
    </section>
  );
}
