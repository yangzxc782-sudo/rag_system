import { isAbort, mergeMessages, qaErrorMessage, QaApiError, qaSessions,
  type ApiResult, type QaMessage, type RequestStatus, type SessionDetail, type TurnInput } from "./qa-sessions";
import { clearPending, readPending, savePending } from "./qa-pending";

type ChatState = {
  session: SessionDetail | null; messages: QaMessage[]; before: number | null;
  ready: boolean; loading: boolean; loadingOlder: boolean; working: boolean; watching: boolean;
  paused: boolean; status: RequestStatus | null; pending: TurnInput | null;
  error: string | null; missingRequest: boolean; retryForbidden: boolean;
};
const initialState = (): ChatState => ({ session: null, messages: [], before: null, ready: false,
  loading: true, loadingOlder: false, working: false, watching: false, paused: false,
  status: null, pending: null, error: null, missingRequest: false, retryForbidden: false });

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new DOMException("View closed", "AbortError")); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, ms);
    if (signal.aborted) abort(); else signal.addEventListener("abort", abort, { once: true });
  });
}

/** One disposable store per mounted thread. No shared answers or memory cache. */
export class ChatSession {
  private state = initialState();
  private listeners = new Set<() => void>();
  private controller = new AbortController();
  private serverState = this.state;
  constructor(readonly threadId: string, private onCommitted: () => void) {}
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getSnapshot = () => this.state;
  getServerSnapshot = () => this.serverState;
  private update(signal: AbortSignal, values: Partial<ChatState>) {
    if (signal.aborted) return;
    this.state = { ...this.state, ...values };
    this.listeners.forEach(fn => fn());
  }
  connect() {
    this.controller.abort();
    this.controller = new AbortController();
    void this.reload();
    return () => this.controller.abort();
  }
  private async history(signal: AbortSignal, replace = false) {
    const { data } = await qaSessions.messages(this.threadId, signal);
    this.update(signal, { messages: replace ? data.items : mergeMessages(this.state.messages, data.items),
      before: replace || this.state.messages.length === 0 ? data.next_before_seq : this.state.before });
  }
  private async accept(response: ApiResult<RequestStatus>, signal: AbortSignal) {
    const status = response.data;
    if (signal.aborted) return;
    if (status.thread_id !== this.threadId || (status.input && status.input.request_id !== status.request_id)) {
      throw new QaApiError("INVALID_RESPONSE");
    }
    const pending = status.input ?? (this.state.pending?.request_id === status.request_id ? this.state.pending : null);
    this.update(signal, { status, pending, missingRequest: false });
    if (signal.aborted) return;
    if (status.status === "completed") {
      clearPending(this.threadId, status.request_id);
      this.update(signal, { pending: null, paused: false });
      // Render only messages read from the committed business history endpoint.
      await this.history(signal);
      if (!signal.aborted) this.onCommitted();
    } else {
      if (pending) savePending(this.threadId, pending);
      // The user message may have committed while the POST response was lost.
      await this.history(signal);
    }
  }
  private async watch(response: ApiResult<RequestStatus>, signal: AbortSignal) {
    await this.accept(response, signal);
    this.update(signal, { watching: true, paused: false });
    try {
      for (let count = 0; count < 10 && response.data.execution_active && response.data.status !== "completed"; count++) {
        await delay(response.retryAfterMs ?? Math.min(10_000, 1000 * 2 ** count), signal);
        response = await qaSessions.status(this.threadId, response.data.request_id, signal,
          response.data.status_url || response.location);
        await this.accept(response, signal);
      }
      this.update(signal, { paused: response.data.execution_active && response.data.status !== "completed" });
    } finally { this.update(signal, { watching: false }); }
  }
  private async lookup(requestId: string, signal: AbortSignal) {
    try { await this.watch(await qaSessions.status(this.threadId, requestId, signal), signal); }
    catch (error) {
      if (!isAbort(error)) this.update(signal, {
        error: qaErrorMessage(error), paused: true,
        missingRequest: error instanceof QaApiError && error.code === "QA_REQUEST_NOT_FOUND",
      });
    }
  }
  async reload() {
    if (this.state.working || this.state.watching) return;
    const signal = this.controller.signal;
    this.update(signal, { loading: true, error: null });
    try {
      const [detail, messages] = await Promise.all([
        qaSessions.detail(this.threadId, signal), qaSessions.messages(this.threadId, signal),
      ]);
      const saved = readPending(this.threadId);
      this.update(signal, { session: detail.data, messages: messages.data.items, before: messages.data.next_before_seq,
        ready: true, loading: false, pending: saved, status: null, missingRequest: false, retryForbidden: false });
      if (detail.data.active_request) {
        const active = detail.data.active_request;
        if (active.thread_id !== this.threadId) throw new QaApiError("INVALID_RESPONSE");
        await this.watch({ ...detail, data: active }, signal);
      } else {
        // A failed latest turn is no longer active, but still has an explicit retry path.
        const last = messages.data.items.at(-1);
        const rid = saved?.request_id ?? (last?.role === "user" && last.status === "failed" ? last.request_id : null);
        if (rid) await this.lookup(rid, signal);
      }
    } catch (error) { if (!isAbort(error)) this.update(signal, { error: qaErrorMessage(error) }); }
    finally { this.update(signal, { loading: false }); }
  }
  async loadOlder() {
    if (!this.state.before || this.state.loadingOlder || this.state.loading) return;
    const signal = this.controller.signal;
    this.update(signal, { loadingOlder: true });
    try {
      const { data } = await qaSessions.messages(this.threadId, signal, this.state.before);
      this.update(signal, { messages: mergeMessages(data.items, this.state.messages), before: data.next_before_seq });
    } catch (error) { if (!isAbort(error)) this.update(signal, { error: qaErrorMessage(error) }); }
    finally { this.update(signal, { loadingOlder: false }); }
  }
  async check(requestId = this.state.status?.request_id ?? this.state.pending?.request_id) {
    if (!requestId || this.state.working || this.state.watching) return;
    const signal = this.controller.signal;
    this.update(signal, { working: true, error: null });
    try { await this.lookup(requestId, signal); }
    finally { this.update(signal, { working: false }); }
  }
  get blocked() {
    return !this.state.ready || this.state.loading || this.state.working || this.state.watching
      || ["running", "finalizing", "needs_recovery"].includes(this.state.status?.status ?? "")
      || Boolean(this.state.pending && !this.state.status);
  }
  get canRetry() {
    return Boolean(this.state.pending && !this.state.retryForbidden && !this.state.working && !this.state.watching
      && (this.state.status?.can_retry || this.state.missingRequest));
  }
  async retry() {
    if (!this.canRetry || !this.state.pending) return false;
    return this.post(this.state.pending);
  }
  async send(question: string, limit: number, documentId: string | null) {
    if (this.blocked) return false;
    return this.post({ request_id: crypto.randomUUID(), question, limit, document_id: documentId });
  }
  private async post(input: TurnInput) {
    const signal = this.controller.signal;
    // Save before fetch; repeated clicks cannot change the frozen request body.
    savePending(this.threadId, input);
    this.update(signal, { working: true, pending: input, status: null, error: null, missingRequest: false, paused: false, retryForbidden: false });
    try { await this.watch(await qaSessions.submit(this.threadId, input, signal), signal); }
    catch (error) {
      if (!isAbort(error)) {
        this.update(signal, { error: qaErrorMessage(error) });
        if (!(error instanceof QaApiError && error.code === "INVALID_RESPONSE")) await this.lookup(input.request_id, signal);
        if (error instanceof QaApiError && error.code === "THREAD_BUSY" && !signal.aborted) {
          const detail = await qaSessions.detail(this.threadId, signal).catch(() => null);
          if (detail?.data.active_request) {
            try { await this.watch({ ...detail, data: detail.data.active_request }, signal); }
            catch (statusError) { if (!isAbort(statusError)) this.update(signal, { error: qaErrorMessage(statusError), paused: true }); }
          }
        }
        if (error instanceof QaApiError && error.code === "IDEMPOTENCY_CONFLICT") {
          this.update(signal, { error: qaErrorMessage(error), retryForbidden: true });
        }
      }
    } finally { this.update(signal, { working: false }); }
    return !signal.aborted && this.state.status?.status === "completed";
  }
}
