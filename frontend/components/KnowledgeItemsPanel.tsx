"use client";

import type { FormEvent } from "react";
import { useEffect, useState } from "react";

import {
  approveKnowledgeItem,
  deprecateKnowledgeItem,
  extractKnowledgeItems,
  friendlyKnowledgeItemErrorMessage,
  getKnowledgeItem,
  getKnowledgeItemChunks,
  getKnowledgeItemReviews,
  getKnowledgeItemVersions,
  listKnowledgeItems,
  rejectKnowledgeItem,
  reviseKnowledgeItem,
  submitKnowledgeItem,
  updateKnowledgeItem,
  type ApiEnvelope,
  type KnowledgeExtractionData,
  type KnowledgeExtractionRequest,
  type KnowledgeItemChunkData,
  type KnowledgeItemData,
  type KnowledgeItemListData,
  type KnowledgeItemReviewData,
  type KnowledgeItemStatus,
  type KnowledgeItemType,
  type KnowledgeItemUpdatePayload,
  type KnowledgeItemVersionData,
} from "@/lib/knowledge-items";

const STATUS_OPTIONS: Array<KnowledgeItemStatus | ""> = [
  "",
  "draft",
  "pending_review",
  "approved",
  "rejected",
  "deprecated",
];

const ITEM_TYPES: KnowledgeItemType[] = [
  "process_rule",
  "parameter_recommendation",
  "defect_cause",
  "defect_solution",
  "material_property",
  "standard_requirement",
  "term_definition",
  "case_experience",
];

const STATUS_LABELS: Record<string, string> = {
  draft: "draft",
  pending_review: "pending_review",
  approved: "approved",
  rejected: "rejected",
  deprecated: "deprecated",
};

function statusBadgeClass(status: string): string {
  if (status === "approved") {
    return "border-emerald-200 bg-emerald-50 text-emerald-800";
  }
  if (status === "pending_review") {
    return "border-amber-200 bg-amber-50 text-amber-800";
  }
  if (status === "rejected") {
    return "border-rose-200 bg-rose-50 text-rose-800";
  }
  if (status === "deprecated") {
    return "border-slate-300 bg-slate-100 text-slate-500";
  }
  return "border-slate-200 bg-slate-50 text-slate-700";
}

function formatDate(value: string | null | undefined): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(value);
}

function jsonText(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  return JSON.stringify(value, null, 2);
}

function displayJson(value: unknown): string {
  const text = jsonText(value);
  return text || "-";
}

function parseJsonField(value: string, field: string): unknown | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    throw new Error(`${field} 不是合法 JSON。`);
  }
}

function splitTokens(value: string): string[] {
  return value
    .split(/[\s,，]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function responseError<T>(response: ApiEnvelope<T>, fallback: string): string {
  return response.error ? friendlyKnowledgeItemErrorMessage(response.error) : fallback;
}

function canEdit(item: KnowledgeItemData | null): boolean {
  return item?.status === "draft" || item?.status === "rejected";
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`rounded-md border px-2 py-1 text-xs font-medium ${statusBadgeClass(status)}`}>
      {STATUS_LABELS[status] ?? status}
    </span>
  );
}

function Field({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div>
      <dt className="text-xs font-medium text-slate-500">{label}</dt>
      <dd className="mt-1 break-words text-sm text-slate-900">{value ?? "-"}</dd>
    </div>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-56 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3 text-xs leading-5 text-slate-700">
      {displayJson(value)}
    </pre>
  );
}

export default function KnowledgeItemsPanel() {
  const [statusFilter, setStatusFilter] = useState("");
  const [itemTypeFilter, setItemTypeFilter] = useState("");
  const [sourceFilenameFilter, setSourceFilenameFilter] = useState("");
  const [sourceDocumentIdFilter, setSourceDocumentIdFilter] = useState("");
  const [limit, setLimit] = useState(20);
  const [offset, setOffset] = useState(0);

  const [listData, setListData] = useState<KnowledgeItemListData | null>(null);
  const [selectedItem, setSelectedItem] = useState<KnowledgeItemData | null>(null);
  const [chunks, setChunks] = useState<KnowledgeItemChunkData[]>([]);
  const [versions, setVersions] = useState<KnowledgeItemVersionData[]>([]);
  const [reviews, setReviews] = useState<KnowledgeItemReviewData[]>([]);

  const [isLoadingList, setIsLoadingList] = useState(false);
  const [isLoadingDetail, setIsLoadingDetail] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [isExtracting, setIsExtracting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [editTitle, setEditTitle] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editStructuredData, setEditStructuredData] = useState("");
  const [editEntities, setEditEntities] = useState("");
  const [editParameters, setEditParameters] = useState("");
  const [editConditions, setEditConditions] = useState("");
  const [editConfidence, setEditConfidence] = useState("");
  const [editSourceFilename, setEditSourceFilename] = useState("");
  const [editSourceChunkIds, setEditSourceChunkIds] = useState("");
  const [editChangeReason, setEditChangeReason] = useState("");
  const [editUpdatedBy, setEditUpdatedBy] = useState("");

  const [reviewer, setReviewer] = useState("");
  const [reviewComment, setReviewComment] = useState("");
  const [reviseCreatedBy, setReviseCreatedBy] = useState("");
  const [reviseChangeReason, setReviseChangeReason] = useState("");

  const [extractMode, setExtractMode] = useState<"document" | "chunks">("chunks");
  const [extractDocumentId, setExtractDocumentId] = useState("");
  const [extractChunkIds, setExtractChunkIds] = useState("");
  const [extractItemTypes, setExtractItemTypes] = useState("");
  const [extractAutoSubmit, setExtractAutoSubmit] = useState(false);
  const [extractMaxChunks, setExtractMaxChunks] = useState("");
  const [extractCreatedBy, setExtractCreatedBy] = useState("");
  const [extractResult, setExtractResult] = useState<KnowledgeExtractionData | null>(null);

  useEffect(() => {
    void loadList(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function loadList(nextOffset = offset) {
    setIsLoadingList(true);
    setError(null);
    const normalizedLimit = Math.min(100, Math.max(1, Math.trunc(limit || 20)));
    const response = await listKnowledgeItems({
      status: statusFilter || undefined,
      item_type: itemTypeFilter || undefined,
      source_filename: sourceFilenameFilter.trim() || undefined,
      source_document_id: sourceDocumentIdFilter.trim() || undefined,
      limit: normalizedLimit,
      offset: Math.max(0, nextOffset),
    });
    setIsLoadingList(false);

    if (!response.success || !response.data) {
      setListData(null);
      setError(responseError(response, "知识条目列表加载失败。"));
      return;
    }

    setLimit(response.data.limit);
    setOffset(response.data.offset);
    setListData(response.data);
    if (!selectedItem && response.data.items.length > 0) {
      void loadDetail(response.data.items[0].id);
    }
  }

  async function loadDetail(itemId: string) {
    setIsLoadingDetail(true);
    setError(null);
    const [itemResponse, chunksResponse, versionsResponse, reviewsResponse] = await Promise.all([
      getKnowledgeItem(itemId),
      getKnowledgeItemChunks(itemId),
      getKnowledgeItemVersions(itemId),
      getKnowledgeItemReviews(itemId),
    ]);
    setIsLoadingDetail(false);

    if (!itemResponse.success || !itemResponse.data) {
      setError(responseError(itemResponse, "知识条目详情加载失败。"));
      return;
    }

    setSelectedItem(itemResponse.data);
    setChunks(chunksResponse.success && chunksResponse.data ? chunksResponse.data.items : []);
    setVersions(versionsResponse.success && versionsResponse.data ? versionsResponse.data.items : []);
    setReviews(reviewsResponse.success && reviewsResponse.data ? reviewsResponse.data.items : []);
    hydrateEditForm(itemResponse.data);

    const secondaryError = [chunksResponse, versionsResponse, reviewsResponse].find((response) => !response.success);
    if (secondaryError) {
      setError(responseError(secondaryError, "部分详情数据加载失败。"));
    }
  }

  function hydrateEditForm(item: KnowledgeItemData) {
    setEditTitle(item.title);
    setEditContent(item.content);
    setEditStructuredData(jsonText(item.structured_data));
    setEditEntities(jsonText(item.entities));
    setEditParameters(jsonText(item.parameters));
    setEditConditions(jsonText(item.conditions));
    setEditConfidence(item.confidence === null || item.confidence === undefined ? "" : String(item.confidence));
    setEditSourceFilename(item.source_filename ?? "");
    setEditSourceChunkIds((item.source_chunk_ids ?? []).join("\n"));
    setEditChangeReason("");
    setEditUpdatedBy("");
  }

  async function refreshAfterChange(itemId: string) {
    await loadList(offset);
    await loadDetail(itemId);
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedItem || !canEdit(selectedItem)) {
      setError("当前状态不允许直接编辑。");
      return;
    }

    let payload: KnowledgeItemUpdatePayload;
    try {
      const confidence = editConfidence.trim() ? Number(editConfidence) : null;
      if (confidence !== null && (!Number.isFinite(confidence) || confidence < 0 || confidence > 1)) {
        throw new Error("confidence 必须在 0 到 1 之间。");
      }
      const sourceChunkIds = splitTokens(editSourceChunkIds);
      payload = {
        title: editTitle,
        content: editContent,
        structured_data: parseJsonField(editStructuredData, "structured_data"),
        entities: parseJsonField(editEntities, "entities"),
        parameters: parseJsonField(editParameters, "parameters"),
        conditions: parseJsonField(editConditions, "conditions"),
        confidence,
        source_filename: editSourceFilename.trim() || null,
        source_chunk_ids: sourceChunkIds.length > 0 ? sourceChunkIds : null,
        updated_by: editUpdatedBy.trim() || null,
        change_reason: editChangeReason.trim() || null,
      };
    } catch (parseError) {
      setError(parseError instanceof Error ? parseError.message : "编辑表单解析失败。");
      return;
    }

    setIsSaving(true);
    setError(null);
    const response = await updateKnowledgeItem(selectedItem.id, payload);
    setIsSaving(false);

    if (!response.success || !response.data) {
      setError(responseError(response, "知识条目保存失败。"));
      return;
    }

    setNotice("知识条目已保存。");
    await refreshAfterChange(response.data.id);
  }

  async function runReviewAction(action: "submit" | "approve" | "reject" | "deprecate") {
    if (!selectedItem) {
      return;
    }
    setActionBusy(action);
    setError(null);
    const payload = {
      reviewer: reviewer.trim() || null,
      review_comment: reviewComment.trim() || null,
    };
    const response =
      action === "submit"
        ? await submitKnowledgeItem(selectedItem.id, payload)
        : action === "approve"
          ? await approveKnowledgeItem(selectedItem.id, payload)
          : action === "reject"
            ? await rejectKnowledgeItem(selectedItem.id, payload)
            : await deprecateKnowledgeItem(selectedItem.id, payload);
    setActionBusy(null);

    if (!response.success || !response.data) {
      setError(responseError(response, "审核操作失败。"));
      return;
    }

    setNotice(`审核操作已完成：${action}。`);
    await refreshAfterChange(response.data.id);
  }

  async function handleRevise() {
    if (!selectedItem) {
      return;
    }
    setActionBusy("revise");
    setError(null);
    const response = await reviseKnowledgeItem(selectedItem.id, {
      created_by: reviseCreatedBy.trim() || null,
      change_reason: reviseChangeReason.trim() || null,
    });
    setActionBusy(null);

    if (!response.success || !response.data) {
      setError(responseError(response, "创建修订版失败。"));
      return;
    }

    setNotice("已创建新的 draft 修订版。");
    await loadList(offset);
    await loadDetail(response.data.id);
  }

  async function handleExtract(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setExtractResult(null);

    const chunkIds = splitTokens(extractChunkIds);
    const itemTypes = splitTokens(extractItemTypes);
    const maxChunks = extractMaxChunks.trim() ? Number(extractMaxChunks) : undefined;

    if (extractMode === "document" && !extractDocumentId.trim()) {
      setError("mode=document 时必须填写 document_id。");
      return;
    }
    if (extractMode === "chunks" && chunkIds.length === 0) {
      setError("mode=chunks 时必须填写 chunk_ids。");
      return;
    }
    if (maxChunks !== undefined && (!Number.isFinite(maxChunks) || maxChunks <= 0)) {
      setError("max_chunks 必须大于 0。");
      return;
    }

    const payload: KnowledgeExtractionRequest = {
      mode: extractMode,
      document_id: extractDocumentId.trim() || null,
      chunk_ids: extractMode === "chunks" ? chunkIds : undefined,
      item_types: itemTypes.length > 0 ? itemTypes : undefined,
      auto_submit: extractAutoSubmit,
      max_chunks: maxChunks ? Math.trunc(maxChunks) : undefined,
      created_by: extractCreatedBy.trim() || null,
    };

    setIsExtracting(true);
    const response = await extractKnowledgeItems(payload);
    setIsExtracting(false);

    if (!response.success || !response.data) {
      setError(responseError(response, "知识条目抽取失败。"));
      return;
    }

    setExtractResult(response.data);
    setNotice(`抽取完成，创建 ${response.data.created} 条知识条目。`);
    await loadList(0);
    if (response.data.items.length > 0) {
      await loadDetail(response.data.items[0].id);
    }
  }

  const total = listData?.total ?? 0;
  const canGoPrevious = offset > 0;
  const canGoNext = offset + limit < total;

  return (
    <div className="grid gap-5">
      {error ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          {error}
        </div>
      ) : null}
      {notice ? (
        <div className="rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
          {notice}
        </div>
      ) : null}

      <section className="rounded-md border border-slate-200 bg-white shadow-sm">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void loadList(0);
          }}
          className="grid gap-4 border-b border-slate-200 px-5 py-4 sm:grid-cols-2 xl:grid-cols-[minmax(140px,180px)_minmax(180px,220px)_minmax(220px,280px)_minmax(260px,1fr)_minmax(90px,120px)_minmax(140px,180px)] xl:items-end"
        >
          <label className="grid min-w-0 gap-2">
            <span className="text-sm font-medium text-slate-700">status</span>
            <select
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
            >
              {STATUS_OPTIONS.map((status) => (
                <option key={status || "all"} value={status}>
                  {status || "全部"}
                </option>
              ))}
            </select>
          </label>
          <label className="grid min-w-0 gap-2">
            <span className="text-sm font-medium text-slate-700">item_type</span>
            <select
              value={itemTypeFilter}
              onChange={(event) => setItemTypeFilter(event.target.value)}
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
            >
              <option value="">全部</option>
              {ITEM_TYPES.map((itemType) => (
                <option key={itemType} value={itemType}>
                  {itemType}
                </option>
              ))}
            </select>
          </label>
          <label className="grid min-w-0 gap-2">
            <span className="text-sm font-medium text-slate-700">source_filename</span>
            <input
              value={sourceFilenameFilter}
              onChange={(event) => setSourceFilenameFilter(event.target.value)}
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
            />
          </label>
          <label className="grid min-w-0 gap-2">
            <span className="text-sm font-medium text-slate-700">source_document_id</span>
            <input
              value={sourceDocumentIdFilter}
              onChange={(event) => setSourceDocumentIdFilter(event.target.value)}
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
            />
          </label>
          <label className="grid min-w-0 gap-2">
            <span className="text-sm font-medium text-slate-700">limit</span>
            <input
              type="number"
              min={1}
              max={100}
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
            />
          </label>
          <button
            type="submit"
            disabled={isLoadingList}
            className="w-full rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-400"
          >
            {isLoadingList ? "加载中..." : "刷新列表"}
          </button>
        </form>

        <div className="flex flex-col gap-3 px-5 py-3 text-sm text-slate-600 sm:flex-row sm:items-center sm:justify-between">
          <span>共 {total} 条</span>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={!canGoPrevious || isLoadingList}
              onClick={() => void loadList(Math.max(0, offset - limit))}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:text-slate-400"
            >
              上一页
            </button>
            <button
              type="button"
              disabled={!canGoNext || isLoadingList}
              onClick={() => void loadList(offset + limit)}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:text-slate-400"
            >
              下一页
            </button>
          </div>
        </div>

        {listData && listData.items.length > 0 ? (
          <div className="divide-y divide-slate-100">
            {listData.items.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => void loadDetail(item.id)}
                className={[
                  "grid w-full gap-3 px-5 py-4 text-left transition hover:bg-slate-50",
                  "lg:grid-cols-[minmax(0,1fr)_180px_140px_120px]",
                  selectedItem?.id === item.id ? "bg-slate-50" : "",
                ].join(" ")}
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge status={item.status} />
                    <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700">
                      {item.item_type}
                    </span>
                  </div>
                  <p className="mt-2 break-words text-sm font-semibold text-slate-950">{item.title}</p>
                  <p className="mt-1 line-clamp-2 break-words text-sm leading-6 text-slate-600">{item.content}</p>
                </div>
                <div className="text-sm text-slate-600">
                  <p className="text-xs font-medium text-slate-500">source_filename</p>
                  <p className="mt-1 break-words">{item.source_filename ?? "-"}</p>
                </div>
                <div className="text-sm text-slate-600">
                  <p className="text-xs font-medium text-slate-500">confidence</p>
                  <p className="mt-1">{formatConfidence(item.confidence)}</p>
                </div>
                <div className="text-sm text-slate-600">
                  <p className="text-xs font-medium text-slate-500">version</p>
                  <p className="mt-1">{item.version}</p>
                  <p className="mt-1 text-xs">{formatDate(item.updated_at)}</p>
                </div>
              </button>
            ))}
          </div>
        ) : (
          <div className="border-t border-slate-100 px-5 py-10 text-center text-sm text-slate-500">
            暂无知识条目。
          </div>
        )}
      </section>

      <section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_420px]">
        <div className="rounded-md border border-slate-200 bg-white shadow-sm">
          <div className="border-b border-slate-200 px-5 py-4">
            <h3 className="text-lg font-semibold text-slate-950">详情</h3>
          </div>

          {!selectedItem ? (
            <div className="px-5 py-10 text-center text-sm text-slate-500">请选择一条知识条目。</div>
          ) : (
            <div className="grid gap-5 px-5 py-4">
              {isLoadingDetail ? <p className="text-sm text-slate-500">详情加载中...</p> : null}
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={selectedItem.status} />
                <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700">
                  {selectedItem.item_type}
                </span>
                <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700">
                  version {selectedItem.version}
                </span>
              </div>

              <div>
                <h4 className="break-words text-xl font-semibold text-slate-950">{selectedItem.title}</h4>
                <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7 text-slate-800">
                  {selectedItem.content}
                </p>
              </div>

              <dl className="grid gap-4 md:grid-cols-2">
                <Field label="id" value={selectedItem.id} />
                <Field label="content_hash" value={selectedItem.content_hash} />
                <Field label="source_document_id" value={selectedItem.source_document_id} />
                <Field label="source_filename" value={selectedItem.source_filename} />
                <Field label="source_chunk_ids" value={(selectedItem.source_chunk_ids ?? []).join(", ") || "-"} />
                <Field label="reviewed_by" value={selectedItem.reviewed_by} />
                <Field label="review_comment" value={selectedItem.review_comment} />
                <Field label="reviewed_at" value={formatDate(selectedItem.reviewed_at)} />
                <Field label="revises_item_id" value={selectedItem.revises_item_id} />
                <Field label="created_at" value={formatDate(selectedItem.created_at)} />
                <Field label="updated_at" value={formatDate(selectedItem.updated_at)} />
                <div>
                  <dt className="text-xs font-medium text-slate-500">confidence</dt>
                  <dd className="mt-1 text-sm text-slate-900">{formatConfidence(selectedItem.confidence)}</dd>
                  <p className="mt-1 text-xs text-slate-500">抽取置信度，不等于审核可信度。</p>
                </div>
              </dl>

              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <p className="mb-2 text-sm font-semibold text-slate-950">structured_data</p>
                  <JsonBlock value={selectedItem.structured_data} />
                </div>
                <div>
                  <p className="mb-2 text-sm font-semibold text-slate-950">entities</p>
                  <JsonBlock value={selectedItem.entities} />
                </div>
                <div>
                  <p className="mb-2 text-sm font-semibold text-slate-950">parameters</p>
                  <JsonBlock value={selectedItem.parameters} />
                </div>
                <div>
                  <p className="mb-2 text-sm font-semibold text-slate-950">conditions</p>
                  <JsonBlock value={selectedItem.conditions} />
                </div>
              </div>

              <section className="border-t border-slate-200 pt-4">
                <h4 className="text-sm font-semibold text-slate-950">source chunks / source_text</h4>
                <p className="mt-1 text-xs leading-5 text-slate-500">
                  source_text 是抽取时使用的原文快照，不替代 document_chunks.content，用于审核和后续来源追溯。
                </p>
                <div className="mt-3 divide-y divide-slate-100 rounded-md border border-slate-200">
                  {chunks.length > 0 ? (
                    chunks.map((chunk) => (
                      <article key={chunk.id} className="px-4 py-3">
                        <dl className="grid gap-2 text-xs text-slate-600 md:grid-cols-3">
                          <Field label="chunk_id" value={chunk.chunk_id} />
                          <Field label="document_id" value={chunk.document_id} />
                          <Field label="chunk_index" value={chunk.chunk_index} />
                        </dl>
                        <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-slate-800">
                          {chunk.source_text}
                        </p>
                        <p className="mt-2 text-xs text-slate-500">{formatDate(chunk.created_at)}</p>
                      </article>
                    ))
                  ) : (
                    <p className="px-4 py-6 text-center text-sm text-slate-500">暂无来源片段。</p>
                  )}
                </div>
              </section>

              <section className="grid gap-4 border-t border-slate-200 pt-4 lg:grid-cols-2">
                <div>
                  <h4 className="text-sm font-semibold text-slate-950">versions</h4>
                  <div className="mt-3 divide-y divide-slate-100 rounded-md border border-slate-200">
                    {versions.length > 0 ? (
                      versions.map((version) => (
                        <article key={version.id} className="px-4 py-3">
                          <div className="flex flex-wrap gap-2 text-xs text-slate-600">
                            <span>version {version.version}</span>
                            <span>{version.created_by ?? "-"}</span>
                            <span>{formatDate(version.created_at)}</span>
                          </div>
                          <p className="mt-2 text-xs text-slate-500">{version.change_reason ?? "-"}</p>
                          <div className="mt-3">
                            <JsonBlock value={version.snapshot} />
                          </div>
                        </article>
                      ))
                    ) : (
                      <p className="px-4 py-6 text-center text-sm text-slate-500">暂无版本快照。</p>
                    )}
                  </div>
                </div>

                <div>
                  <h4 className="text-sm font-semibold text-slate-950">reviews</h4>
                  <div className="mt-3 divide-y divide-slate-100 rounded-md border border-slate-200">
                    {reviews.length > 0 ? (
                      reviews.map((review) => (
                        <article key={review.id} className="px-4 py-3 text-sm">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-semibold text-slate-950">{review.review_action}</span>
                            <span className="text-slate-500">
                              {review.from_status} → {review.to_status}
                            </span>
                          </div>
                          <p className="mt-2 text-slate-700">{review.review_comment ?? "-"}</p>
                          <p className="mt-2 text-xs text-slate-500">
                            {review.reviewer ?? "-"} · {formatDate(review.created_at)}
                          </p>
                        </article>
                      ))
                    ) : (
                      <p className="px-4 py-6 text-center text-sm text-slate-500">暂无审核记录。</p>
                    )}
                  </div>
                </div>
              </section>
            </div>
          )}
        </div>

        <div className="grid gap-5">
          <section className="rounded-md border border-slate-200 bg-white shadow-sm">
            <div className="border-b border-slate-200 px-5 py-4">
              <h3 className="text-lg font-semibold text-slate-950">编辑</h3>
              {selectedItem ? (
                <p className="mt-1 text-sm text-slate-500">
                  {canEdit(selectedItem) ? "draft / rejected 可编辑。" : "pending_review / approved / deprecated 禁止直接编辑。"}
                </p>
              ) : null}
            </div>
            <form onSubmit={handleSave} className="grid gap-3 px-5 py-4">
              <label className="grid gap-1">
                <span className="text-sm font-medium text-slate-700">title</span>
                <input
                  value={editTitle}
                  disabled={!canEdit(selectedItem)}
                  onChange={(event) => setEditTitle(event.target.value)}
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500 disabled:bg-slate-100"
                />
              </label>
              <label className="grid gap-1">
                <span className="text-sm font-medium text-slate-700">content</span>
                <textarea
                  rows={5}
                  value={editContent}
                  disabled={!canEdit(selectedItem)}
                  onChange={(event) => setEditContent(event.target.value)}
                  className={[
                    "resize-y rounded-md border border-slate-300 px-3 py-2 text-sm leading-6",
                    "outline-none focus:border-slate-500 disabled:bg-slate-100",
                  ].join(" ")}
                />
              </label>
              <label className="grid gap-1">
                <span className="text-sm font-medium text-slate-700">confidence</span>
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.0001}
                  value={editConfidence}
                  disabled={!canEdit(selectedItem)}
                  onChange={(event) => setEditConfidence(event.target.value)}
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500 disabled:bg-slate-100"
                />
              </label>
              <label className="grid gap-1">
                <span className="text-sm font-medium text-slate-700">source_filename</span>
                <input
                  value={editSourceFilename}
                  disabled={!canEdit(selectedItem)}
                  onChange={(event) => setEditSourceFilename(event.target.value)}
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500 disabled:bg-slate-100"
                />
              </label>
              <label className="grid gap-1">
                <span className="text-sm font-medium text-slate-700">source_chunk_ids</span>
                <textarea
                  rows={3}
                  value={editSourceChunkIds}
                  disabled={!canEdit(selectedItem)}
                  onChange={(event) => setEditSourceChunkIds(event.target.value)}
                  className={[
                    "resize-y rounded-md border border-slate-300 px-3 py-2 text-sm leading-6",
                    "outline-none focus:border-slate-500 disabled:bg-slate-100",
                  ].join(" ")}
                />
              </label>
              {[
                ["structured_data", editStructuredData, setEditStructuredData],
                ["entities", editEntities, setEditEntities],
                ["parameters", editParameters, setEditParameters],
                ["conditions", editConditions, setEditConditions],
              ].map(([label, value, setter]) => (
                <label key={label as string} className="grid gap-1">
                  <span className="text-sm font-medium text-slate-700">{label as string}</span>
                  <textarea
                    rows={3}
                    value={value as string}
                    disabled={!canEdit(selectedItem)}
                    onChange={(event) => (setter as (next: string) => void)(event.target.value)}
                    className={[
                      "resize-y rounded-md border border-slate-300 px-3 py-2 font-mono text-xs leading-5",
                      "outline-none focus:border-slate-500 disabled:bg-slate-100",
                    ].join(" ")}
                  />
                </label>
              ))}
              <label className="grid gap-1">
                <span className="text-sm font-medium text-slate-700">updated_by</span>
                <input
                  value={editUpdatedBy}
                  disabled={!canEdit(selectedItem)}
                  onChange={(event) => setEditUpdatedBy(event.target.value)}
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500 disabled:bg-slate-100"
                />
              </label>
              <label className="grid gap-1">
                <span className="text-sm font-medium text-slate-700">change_reason</span>
                <input
                  value={editChangeReason}
                  disabled={!canEdit(selectedItem)}
                  onChange={(event) => setEditChangeReason(event.target.value)}
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500 disabled:bg-slate-100"
                />
              </label>
              <button
                type="submit"
                disabled={!canEdit(selectedItem) || isSaving}
                className={[
                  "rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white",
                  "disabled:cursor-not-allowed disabled:bg-slate-400",
                ].join(" ")}
              >
                {isSaving ? "保存中..." : "保存编辑"}
              </button>
            </form>
          </section>

          <section className="rounded-md border border-slate-200 bg-white shadow-sm">
            <div className="border-b border-slate-200 px-5 py-4">
              <h3 className="text-lg font-semibold text-slate-950">审核操作</h3>
            </div>
            <div className="grid gap-3 px-5 py-4">
              <label className="grid gap-1">
                <span className="text-sm font-medium text-slate-700">reviewer</span>
                <input
                  value={reviewer}
                  onChange={(event) => setReviewer(event.target.value)}
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
                />
              </label>
              <label className="grid gap-1">
                <span className="text-sm font-medium text-slate-700">review_comment</span>
                <textarea
                  rows={3}
                  value={reviewComment}
                  onChange={(event) => setReviewComment(event.target.value)}
                  className="resize-y rounded-md border border-slate-300 px-3 py-2 text-sm leading-6 outline-none focus:border-slate-500"
                />
              </label>
              <div className="flex flex-wrap gap-2">
                {selectedItem?.status === "draft" || selectedItem?.status === "rejected" ? (
                  <button
                    type="button"
                    disabled={actionBusy !== null}
                    onClick={() => void runReviewAction("submit")}
                    className="rounded-md bg-slate-950 px-3 py-2 text-sm font-medium text-white disabled:bg-slate-400"
                  >
                    {actionBusy === "submit" ? "提交中..." : "submit"}
                  </button>
                ) : null}
                {selectedItem?.status === "pending_review" ? (
                  <>
                    <button
                      type="button"
                      disabled={actionBusy !== null}
                      onClick={() => void runReviewAction("approve")}
                      className="rounded-md bg-emerald-700 px-3 py-2 text-sm font-medium text-white disabled:bg-slate-400"
                    >
                      {actionBusy === "approve" ? "批准中..." : "approve"}
                    </button>
                    <button
                      type="button"
                      disabled={actionBusy !== null}
                      onClick={() => void runReviewAction("reject")}
                      className="rounded-md bg-rose-700 px-3 py-2 text-sm font-medium text-white disabled:bg-slate-400"
                    >
                      {actionBusy === "reject" ? "驳回中..." : "reject"}
                    </button>
                  </>
                ) : null}
                {selectedItem?.status === "approved" ? (
                  <button
                    type="button"
                    disabled={actionBusy !== null}
                    onClick={() => void runReviewAction("deprecate")}
                    className="rounded-md bg-slate-700 px-3 py-2 text-sm font-medium text-white disabled:bg-slate-400"
                  >
                    {actionBusy === "deprecate" ? "废弃中..." : "deprecate"}
                  </button>
                ) : null}
              </div>

              <div className="border-t border-slate-200 pt-4">
                <h4 className="text-sm font-semibold text-slate-950">revise</h4>
                <div className="mt-3 grid gap-3">
                  <input
                    value={reviseCreatedBy}
                    onChange={(event) => setReviseCreatedBy(event.target.value)}
                    placeholder="created_by"
                    className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
                  />
                  <input
                    value={reviseChangeReason}
                    onChange={(event) => setReviseChangeReason(event.target.value)}
                    placeholder="change_reason"
                    className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
                  />
                  <button
                    type="button"
                    disabled={
                      actionBusy !== null ||
                      !(selectedItem?.status === "approved" || selectedItem?.status === "deprecated")
                    }
                    onClick={() => void handleRevise()}
                    className={[
                      "rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-800",
                      "disabled:cursor-not-allowed disabled:text-slate-400",
                    ].join(" ")}
                  >
                    {actionBusy === "revise" ? "创建中..." : "创建 draft 修订版"}
                  </button>
                </div>
              </div>
            </div>
          </section>
        </div>
      </section>

      <section className="rounded-md border border-slate-200 bg-white shadow-sm">
        <div className="border-b border-slate-200 px-5 py-4">
          <h3 className="text-lg font-semibold text-slate-950">自动抽取</h3>
          <p className="mt-1 text-sm leading-6 text-slate-600">
            auto_submit 默认 false；开启后进入 pending_review，永远不会直接 approved。
          </p>
        </div>
        <form
          onSubmit={handleExtract}
          className="grid gap-4 px-5 py-4 lg:grid-cols-[160px_minmax(0,1fr)_minmax(0,1fr)]"
        >
          <label className="grid gap-2">
            <span className="text-sm font-medium text-slate-700">mode</span>
            <select
              value={extractMode}
              onChange={(event) => setExtractMode(event.target.value as "document" | "chunks")}
              className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
            >
              <option value="chunks">chunks</option>
              <option value="document">document</option>
            </select>
          </label>
          <label className="grid gap-2">
            <span className="text-sm font-medium text-slate-700">document_id</span>
            <input
              value={extractDocumentId}
              onChange={(event) => setExtractDocumentId(event.target.value)}
              className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
            />
          </label>
          <label className="grid gap-2">
            <span className="text-sm font-medium text-slate-700">created_by</span>
            <input
              value={extractCreatedBy}
              onChange={(event) => setExtractCreatedBy(event.target.value)}
              className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
            />
          </label>
          <label className="grid gap-2 lg:col-span-2">
            <span className="text-sm font-medium text-slate-700">chunk_ids</span>
            <textarea
              rows={4}
              value={extractChunkIds}
              onChange={(event) => setExtractChunkIds(event.target.value)}
              className="resize-y rounded-md border border-slate-300 px-3 py-2 text-sm leading-6 outline-none focus:border-slate-500"
            />
          </label>
          <label className="grid gap-2">
            <span className="text-sm font-medium text-slate-700">item_types</span>
            <textarea
              rows={4}
              value={extractItemTypes}
              onChange={(event) => setExtractItemTypes(event.target.value)}
              placeholder="process_rule, defect_solution"
              className="resize-y rounded-md border border-slate-300 px-3 py-2 text-sm leading-6 outline-none focus:border-slate-500"
            />
          </label>
          <label className="grid gap-2">
            <span className="text-sm font-medium text-slate-700">max_chunks</span>
            <input
              type="number"
              min={1}
              value={extractMaxChunks}
              onChange={(event) => setExtractMaxChunks(event.target.value)}
              className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={extractAutoSubmit}
              onChange={(event) => setExtractAutoSubmit(event.target.checked)}
              className="h-4 w-4 rounded border-slate-300"
            />
            auto_submit
          </label>
          <div className="flex items-end">
            <button
              type="submit"
              disabled={isExtracting}
              className="rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-400"
            >
              {isExtracting ? "抽取中..." : "开始抽取"}
            </button>
          </div>
        </form>

        {extractResult ? (
          <div className="border-t border-slate-200 px-5 py-4">
            <div className="grid gap-3 text-sm md:grid-cols-5">
              <Field label="created" value={extractResult.created} />
              <Field label="status" value={extractResult.status} />
              <Field label="auto_submit" value={String(extractResult.auto_submit)} />
              <Field label="llm.provider" value={extractResult.llm?.provider} />
              <Field label="llm.model" value={extractResult.llm?.model} />
            </div>

            <div className="mt-5 grid gap-5 lg:grid-cols-2">
              <div>
                <h4 className="text-sm font-semibold text-slate-950">created items</h4>
                <div className="mt-3 divide-y divide-slate-100 rounded-md border border-slate-200">
                  {extractResult.items.length > 0 ? (
                    extractResult.items.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => void loadDetail(item.id)}
                        className="block w-full px-4 py-3 text-left hover:bg-slate-50"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <StatusBadge status={item.status} />
                          <span className="text-xs text-slate-500">{item.item_type}</span>
                        </div>
                        <p className="mt-2 text-sm font-semibold text-slate-950">{item.title}</p>
                      </button>
                    ))
                  ) : (
                    <p className="px-4 py-6 text-center text-sm text-slate-500">没有新建条目。</p>
                  )}
                </div>
              </div>
              <div>
                <h4 className="text-sm font-semibold text-slate-950">skipped_duplicates</h4>
                <div className="mt-3 divide-y divide-slate-100 rounded-md border border-slate-200">
                  {extractResult.skipped_duplicates.length > 0 ? (
                    extractResult.skipped_duplicates.map((duplicate) => (
                      <article key={duplicate.content_hash} className="px-4 py-3 text-sm">
                        <p className="font-semibold text-slate-950">{duplicate.title}</p>
                        <p className="mt-1 text-xs text-slate-500">{duplicate.item_type}</p>
                        <p className="mt-2 break-all text-xs text-slate-600">{duplicate.content_hash}</p>
                        <p className="mt-1 break-all text-xs text-slate-500">
                          {duplicate.source_document_id ?? "-"} · {duplicate.reason ?? "-"}
                        </p>
                      </article>
                    ))
                  ) : (
                    <p className="px-4 py-6 text-center text-sm text-slate-500">没有重复跳过项。</p>
                  )}
                </div>
              </div>
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
