"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import {
  deleteDocument,
  getDocumentDeletionStatus,
  retryDocumentDeletion,
  type DocumentDeletionApiResult,
  type DocumentDeletionPublicStatus,
  type DocumentDeletionState,
  type DocumentDeletionStatusData,
} from "@/lib/documents";

const NORMAL_POLL_INTERVAL_MS = 2_000;
const ERROR_POLL_INTERVAL_MS = 5_000;

type DocumentDeletionControlsProps = {
  documentId: string;
  initialStatus: DocumentDeletionState;
  completionMode?: "refresh" | "redirect";
};

function isDeletionInProgress(status: DocumentDeletionState | DocumentDeletionPublicStatus): boolean {
  return status === "deleting" || status === "retrying";
}

function formatRetryTime(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return null;
  }

  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(date);
}

export default function DocumentDeletionControls({
  documentId,
  initialStatus,
  completionMode = "refresh",
}: DocumentDeletionControlsProps) {
  const router = useRouter();
  const [status, setStatus] = useState<DocumentDeletionState | DocumentDeletionPublicStatus>(initialStatus);
  const [statusData, setStatusData] = useState<DocumentDeletionStatusData | null>(null);
  const [isConfirmationOpen, setIsConfirmationOpen] = useState(false);
  const [isActionPending, setIsActionPending] = useState(false);
  const [isComplete, setIsComplete] = useState(false);
  const [pollingHalted, setPollingHalted] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const completeDeletion = useCallback(() => {
    setIsComplete(true);
    setPollingHalted(true);
    setMessage("文档已永久删除，正在刷新页面…");
    setError(null);

    if (completionMode === "redirect") {
      router.push("/documents");
      router.refresh();
      return;
    }

    router.refresh();
  }, [completionMode, router]);

  const acceptStatus = useCallback((data: DocumentDeletionStatusData) => {
    setStatus(data.status);
    setStatusData(data);
    setPollingHalted(data.status === "delete_failed");
    setError(null);
  }, []);

  const handleActionError = useCallback((result: DocumentDeletionApiResult) => {
    const code = result.error?.code;

    if (code === "DOCUMENT_DELETION_EXECUTOR_DISABLED") {
      setPollingHalted(true);
      setError("文档删除服务当前未启用，请稍后重试或联系管理员。");
      return;
    }

    if (code === "DOCUMENT_DELETION_IN_PROGRESS") {
      setStatus("deleting");
      setPollingHalted(false);
      setError(null);
      return;
    }

    if (code === "DOCUMENT_DELETE_FAILED" || code === "DOCUMENT_DELETION_RETRY_REQUIRED") {
      setStatus("delete_failed");
      setPollingHalted(true);
      setError("文档删除失败，请手动重试删除。");
      return;
    }

    if (code === "DOCUMENT_DELETION_STATE_INCONSISTENT") {
      setPollingHalted(true);
      setError("文档删除状态异常，请刷新页面；如问题持续，请联系管理员。");
      return;
    }

    setError("文档删除请求暂时失败，请稍后重试。");
  }, []);

  useEffect(() => {
    if (isComplete || pollingHalted || !isDeletionInProgress(status)) {
      return;
    }

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const schedule = (delay: number) => {
      if (!cancelled) {
        timer = setTimeout(poll, delay);
      }
    };

    const poll = async () => {
      const result = await getDocumentDeletionStatus(documentId);
      if (cancelled) {
        return;
      }

      if (result.httpStatus === 204) {
        completeDeletion();
        return;
      }

      if (result.success && result.data) {
        acceptStatus(result.data);
        schedule(NORMAL_POLL_INTERVAL_MS);
        return;
      }

      const code = result.error?.code;
      if (code === "DOCUMENT_DELETION_STATE_INCONSISTENT") {
        setPollingHalted(true);
        setError("文档删除状态异常，请刷新页面；如问题持续，请联系管理员。");
        return;
      }

      if (code === "DOCUMENT_DELETE_FAILED" || code === "DOCUMENT_DELETION_RETRY_REQUIRED") {
        setStatus("delete_failed");
        setPollingHalted(true);
        setError("文档删除失败，请手动重试删除。");
        return;
      }

      setError("删除状态暂时不可用，系统将在 5 秒后重试查询。");
      schedule(ERROR_POLL_INTERVAL_MS);
    };

    schedule(NORMAL_POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (timer !== null) {
        clearTimeout(timer);
      }
    };
  }, [acceptStatus, completeDeletion, documentId, isComplete, pollingHalted, status]);

  useEffect(() => {
    if (!isConfirmationOpen) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !isActionPending) {
        setIsConfirmationOpen(false);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isActionPending, isConfirmationOpen]);

  async function handleDelete() {
    setIsActionPending(true);
    setMessage(null);
    setError(null);

    const result = await deleteDocument(documentId);
    setIsActionPending(false);
    setIsConfirmationOpen(false);

    if (result.httpStatus === 204) {
      completeDeletion();
      return;
    }

    if (result.success && result.data) {
      acceptStatus(result.data);
      return;
    }

    handleActionError(result);
  }

  async function handleRetry() {
    setIsActionPending(true);
    setMessage(null);
    setError(null);

    const result = await retryDocumentDeletion(documentId);
    setIsActionPending(false);

    if (result.httpStatus === 204) {
      completeDeletion();
      return;
    }

    if (result.success && result.data) {
      acceptStatus(result.data);
      return;
    }

    handleActionError(result);
  }

  const retryTime = formatRetryTime(statusData?.next_retry_at);

  return (
    <div className="flex flex-col items-start gap-2">
      {status === "normal" && !isComplete ? (
        <button
          type="button"
          onClick={() => {
            setMessage(null);
            setError(null);
            setIsConfirmationOpen(true);
          }}
          disabled={isActionPending}
          className="rounded-md border border-red-300 bg-white px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-400"
        >
          删除
        </button>
      ) : null}

      {status === "deleting" && !isComplete ? (
        <p className="text-sm font-medium text-amber-700" role="status">
          正在删除…
        </p>
      ) : null}

      {status === "retrying" && !isComplete ? (
        <div className="text-sm text-amber-700" role="status">
          <p className="font-medium">删除暂时失败，系统正在重试…</p>
          {retryTime ? <p className="mt-1 text-xs">下次重试时间：{retryTime}</p> : null}
        </div>
      ) : null}

      {status === "delete_failed" && !isComplete ? (
        <div className="flex flex-col items-start gap-2">
          <p className="text-sm font-medium text-red-700" role="status">
            删除失败
          </p>
          {statusData?.last_error_code ? (
            <p className="text-xs text-slate-500">错误代码：{statusData.last_error_code}</p>
          ) : null}
          <button
            type="button"
            onClick={handleRetry}
            disabled={isActionPending}
            className="rounded-md border border-red-300 bg-white px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-400"
          >
            {isActionPending ? "正在重试…" : "重试删除"}
          </button>
        </div>
      ) : null}

      {message ? (
        <p className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800" role="status">
          {message}
        </p>
      ) : null}

      {error ? (
        <p className="max-w-md rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800" role="alert">
          {error}
        </p>
      ) : null}

      {isConfirmationOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/50 p-4">
          <div
            aria-describedby={`delete-document-description-${documentId}`}
            aria-labelledby={`delete-document-title-${documentId}`}
            aria-modal="true"
            className="w-full max-w-lg rounded-lg bg-white p-6 shadow-xl"
            role="dialog"
          >
            <h2 className="text-lg font-semibold text-slate-950" id={`delete-document-title-${documentId}`}>
              永久删除该文档？
            </h2>
            <div
              className="mt-4 text-sm leading-6 text-slate-700"
              id={`delete-document-description-${documentId}`}
            >
              <p>删除后将同时删除：</p>
              <ul className="mt-2 list-disc space-y-1 pl-5">
                <li>原始文件</li>
                <li>解析结果</li>
                <li>向量数据</li>
                <li>检索索引</li>
                <li>该文档独有的知识条目</li>
              </ul>
              <p className="mt-4 font-medium text-red-700">此操作完成后不可恢复。</p>
            </div>
            <div className="mt-6 flex justify-end gap-3">
              <button
                type="button"
                onClick={() => setIsConfirmationOpen(false)}
                disabled={isActionPending}
                className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
              >
                取消
              </button>
              <button
                type="button"
                onClick={handleDelete}
                disabled={isActionPending}
                className="rounded-md bg-red-700 px-4 py-2 text-sm font-medium text-white hover:bg-red-800 disabled:cursor-not-allowed disabled:bg-red-300"
              >
                {isActionPending ? "正在提交…" : "永久删除"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
